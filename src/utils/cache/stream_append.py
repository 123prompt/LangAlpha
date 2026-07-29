"""Retry policy for a fenced append to a run's event stream.

A single transient socket timeout on one write used to kill an entire turn.
Retrying is safe *here* and nowhere else in the transport, because the frame
carries an explicit ``{event_id}-0`` id: Redis rejects a duplicate, so the id
is at once the consumer's cursor and the write's idempotency fence.

Lives in the cache layer rather than in either caller. The root-lane writer
(``server.services.runs.stream_writer``) and the subagent spill
(``ptc_agent...background_subagent.redis_stream``) need the identical
protocol, and those two layers deliberately keep separate error vocabularies
— so this module raises its own neutral error and each side translates it.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

from redis.exceptions import ResponseError

from src.utils.cache.redis_cache import (
    EventBufferUnavailableError,
    is_pool_exhaustion,
)

logger = logging.getLogger(__name__)


class StreamAppendError(RuntimeError):
    """The frame could not be placed. Fatal to the run (I6) in every caller."""


# The budget is the ONE authoritative bound; the per-attempt and probe caps are
# ceilings carved out of it, never independent knobs. Fixed caps that can
# outrun the budget make the later attempts unreachable for exactly the slow
# failures they exist for — so an attempt is skipped when the budget is spent,
# and says so, rather than being silently unreachable.
#
# The per-attempt ceiling must clear the cache pool's acquire timeout plus a
# real command, or contention alone cancels a write that was merely QUEUED and
# buys a tail probe for something that was never ambiguous. That timeout is
# operator-configurable, so the ceiling is DERIVED from it rather than assuming
# the default — a raised REDIS_POOL_TIMEOUT would otherwise silently turn every
# queued write into a misclassified ambiguous one.
_ATTEMPTS = 3
_ATTEMPT_HEADROOM_S = 1.5
_PROBE_TIMEOUT_S = 2.0
_BUDGET_FLOOR_S = 8.0
_BACKOFF_MIN_S = 0.05
_BACKOFF_MAX_S = 0.2


def _acquire_timeout_s() -> float:
    from src.config.settings import get_redis_pool_timeout

    return get_redis_pool_timeout()


def _attempt_timeout_s() -> float:
    return _acquire_timeout_s() + _ATTEMPT_HEADROOM_S


def _default_budget_s() -> float:
    """Enough budget for every attempt to actually be reachable."""
    return max(
        _BUDGET_FLOOR_S,
        _ATTEMPTS * (_attempt_timeout_s() + _BACKOFF_MAX_S),
    )

# Recovered writes are invisible by design (the run survives), so they are
# counted: a steady trickle means Redis is degrading even while nothing fails.
_recovered_writes = 0


def _note_recovery(label: str, event_id: int, attempt: int, how: str) -> None:
    global _recovered_writes
    _recovered_writes += 1
    logger.info(
        "[EventBuffer] write recovered on attempt %d for %s (id=%d, via=%s)",
        attempt,
        label,
        event_id,
        how,
    )
    if _recovered_writes == 10 or _recovered_writes % 100 == 0:
        logger.warning(
            "[EventBuffer] %d event writes have needed a retry since startup — "
            "Redis is degraded even though no run has failed",
            _recovered_writes,
        )


async def _probe_tail(
    cache, stream_key: str, deadline: float
) -> Optional[tuple[int, Optional[bytes]]]:
    """Best-effort tail read; a failed probe reads as "unknown", not "empty"."""
    budget = min(_PROBE_TIMEOUT_S, deadline - time.monotonic())
    if budget <= 0:
        return None
    try:
        async with asyncio.timeout(budget):
            return await cache.stream_tail(stream_key)
    except Exception:
        return None


async def stream_append_with_retry(
    cache,
    stream_key: str,
    *,
    event_id: int,
    max_size: int,
    stream_event: str | bytes,
    stream_record: str | bytes | None = None,
    label: str = "",
) -> None:
    """Append one frame, surviving a transient Redis blip. Fatal when it can't.

    Failures split by what the server can have seen:

    * **Nothing was sent** (pool exhaustion) — replay the identical write.
    * **The reply was lost** (timeout, reset, or a duplicate-id rejection after
      an earlier ambiguous attempt) — read the tail. Our write landed only when
      the tail carries our id *and* our bytes; the id alone proves nothing,
      because the stream is reset at event 1 and a crashed predecessor's frame
      can sit under the very id being written. Our id with other bytes, or a
      *greater* tail (an auto-id error/run_end frame somebody else appended),
      both mean the frame can no longer be placed — fatal, because calling
      either one success is precisely how a run completes with an archive it
      never wrote. Anything lower means the write is still missing, so retry.

    Retries carry only the bare XADD: repeating the epoch DEL could erase a
    frame this process never wrote, and repeating the retention heal could
    re-immortalize a stream already stamped terminal. Both are safe to drop —
    the fence catches a DEL that was needed but never ran, and the next write
    heals the TTL.
    """
    label = label or stream_key
    payload = (
        stream_event.encode("utf-8")
        if isinstance(stream_event, str)
        else bytes(stream_event)
    )
    budget_s = _default_budget_s()
    attempt_timeout_s = _attempt_timeout_s()
    deadline = time.monotonic() + budget_s
    bare = False
    may_have_landed = False
    last_exc: BaseException | None = None

    # An attempt shorter than the acquire timeout can only ever be cancelled
    # while still queued — and a cancelled attempt is classified ambiguous, so
    # it buys a tail probe and a bare retry for a write that never left the
    # process. Stop on a spent budget instead of manufacturing that ambiguity.
    min_window_s = _acquire_timeout_s()

    for attempt in range(1, _ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining < min_window_s:
            raise StreamAppendError(
                f"transport_lost: event write for {label} exceeded the "
                f"{budget_s}s budget (id={event_id})"
            ) from last_exc
        try:
            async with asyncio.timeout(min(attempt_timeout_s, remaining)):
                await cache.pipelined_event_buffer(
                    stream_key,
                    event_id=event_id,
                    max_size=max_size,
                    stream_event=stream_event,
                    stream_record=stream_record,
                    ttl=None,
                    bare=bare,
                )
        except EventBufferUnavailableError as exc:
            raise StreamAppendError(
                f"transport_lost: event transport unusable for {label} ({exc})"
            ) from exc
        except Exception as exc:
            last_exc = exc
            if not is_pool_exhaustion(exc):
                if not may_have_landed and isinstance(exc, ResponseError):
                    # Nothing ambiguous has happened yet, so a rejected id
                    # means the stream already holds state this run did not
                    # write.
                    raise StreamAppendError(
                        f"transport_lost: {label} rejected id {event_id} on a "
                        f"first attempt ({exc})"
                    ) from exc
                may_have_landed = True
                tail = await _probe_tail(cache, stream_key, deadline)
                if tail is not None:
                    tail_id, tail_event = tail
                    if tail_id == event_id:
                        if tail_event == payload:
                            _note_recovery(label, event_id, attempt, "tail_probe")
                            return
                        # Our id, someone else's bytes. At event 1 that is a
                        # crashed predecessor's frame the epoch DEL never got
                        # to clear; later it is a second writer on this run.
                        # Either way the frame we are holding cannot be placed,
                        # and calling it landed would archive a frame this run
                        # never wrote.
                        raise StreamAppendError(
                            f"transport_lost: {label} already holds id "
                            f"{event_id} with a different frame; this write "
                            "cannot be placed"
                        ) from exc
                    if tail_id > event_id:
                        raise StreamAppendError(
                            f"transport_lost: {label} tail {tail_id} is past id "
                            f"{event_id}; another writer owns this stream and "
                            "the frame cannot be placed"
                        ) from exc
                bare = True
            if attempt < _ATTEMPTS:
                await asyncio.sleep(
                    random.uniform(_BACKOFF_MIN_S, _BACKOFF_MAX_S)
                )
        else:
            if attempt > 1:
                _note_recovery(label, event_id, attempt, "rewrite")
            return

    raise StreamAppendError(
        f"transport_lost: event write for {label} failed after {_ATTEMPTS} "
        f"attempts (id={event_id}): {last_exc}"
    ) from last_exc
