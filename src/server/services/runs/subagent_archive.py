"""Read side of a subagent's captured-event archive.

The per-task capture stream is written by the agent-side spill and read back
here once, post-terminal, to rebuild the subagent's history. Separated from
the collection lifecycle because it is the one piece with its own failure
contract: an archive that cannot be read to the end must be refused whole,
never served as a prefix.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

from src.utils.cache.redis_cache import get_cache_client

logger = logging.getLogger(__name__)


class SubagentArchiveReadError(RuntimeError):
    """The captured-event archive could not be read to the end.

    Raised rather than yielding a prefix: a truncated archive is
    indistinguishable from a short one at the call site, so persisting half a
    subagent's history — and the usage totals derived from it — would look
    like success.
    """


# Entries per XRANGE round. Reading the whole stream in one call topped the
# SLOWLOG on a busy deployment (MAXLEN allows 300k entries, hundreds of MB) and
# pinned a connection for the length of the transfer.
_ARCHIVE_PAGE = 1000


async def iter_subagent_events_full(
    thread_id: str, task
) -> AsyncIterator[dict]:
    """Yield every captured record for a subagent in seq order.

    Raises ``SubagentArchiveReadError`` if the stream cannot be read in full.
    """
    if task is None or not thread_id:
        return

    high_water = int(getattr(task, "captured_event_seq", 0) or 0)
    if high_water <= 0:
        return

    try:
        cache = get_cache_client()
    except Exception as exc:
        logger.warning(
            "[SubagentCollector] Failed to obtain cache client for "
            f"task {getattr(task, 'task_id', '?')}: {exc}"
        )
        return
    if cache is None or not getattr(cache, "enabled", False) or cache.client is None:
        return

    sa_stream_key = f"subagent:stream:{thread_id}:{task.task_id}"
    # The v1 spill writes explicit ``{seq}-0`` ids, so the high-water mark is
    # directly expressible as a range bound — everything past it is a later
    # epoch's refill and was never ours to read.
    upper = f"{high_water}-0"
    cursor = "-"
    yielded = 0
    while True:
        try:
            page = await cache.client.xrange(
                sa_stream_key, min=cursor, max=upper, count=_ARCHIVE_PAGE
            )
        except Exception as exc:
            raise SubagentArchiveReadError(
                f"XRANGE failed for {sa_stream_key}: {exc}"
            ) from exc

        for entry_id, fields in page or []:
            try:
                seq_part = (
                    entry_id.decode("utf-8")
                    if isinstance(entry_id, bytes)
                    else entry_id
                )
                seq = int(seq_part.split("-", 1)[0])
            except (ValueError, AttributeError):
                continue
            if seq <= 0 or seq > high_water:
                continue
            raw = fields.get(b"record")
            if raw is None:
                continue
            try:
                payload = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                record = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            yielded += 1
            yield record

        # A short page means the range is exhausted. Looping until an EMPTY
        # page instead would spin forever whenever the final page happens to
        # land exactly on the boundary.
        if not page or len(page) < _ARCHIVE_PAGE:
            break
        last_id = page[-1][0]
        if isinstance(last_id, bytes):
            last_id = last_id.decode("utf-8")
        cursor = f"({last_id}"  # exclusive, so the last entry isn't re-read

    expected = high_water
    if yielded < expected:
        logger.warning(
            "subagent_history_truncated",
            extra={
                "thread_id": thread_id,
                "task_id": getattr(task, "task_id", None),
                "expected": expected,
                "recovered": yielded,
                "missing": expected - yielded,
                "redis_write_failed": bool(getattr(task, "redis_write_failed", False)),
            },
        )
