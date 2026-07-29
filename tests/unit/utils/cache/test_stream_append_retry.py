"""Retry protocol for one event-stream append.

A single 5s socket timeout on one write used to kill a whole turn. Retrying is
only safe because the explicit ``{event_id}-0`` id fences duplicates — these
tests pin the classification that rests on that fence, and above all the two
branches where a naive retry would violate I6: a tail PAST our id means another
writer appended, and a tail AT our id carrying other bytes means a predecessor's
frame is sitting there. Calling either "success" completes a run whose archive
it never wrote.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import redis.exceptions as redis_exceptions

from src.utils.cache.redis_cache import EventBufferUnavailableError
from src.utils.cache.stream_append import (
    StreamAppendError,
    stream_append_with_retry,
)


FRAME = "id: 5\ndata: x\n\n"
OURS = FRAME.encode("utf-8")


def _cache(**overrides) -> MagicMock:
    cache = MagicMock()
    cache.enabled = True
    cache.pipelined_event_buffer = AsyncMock(return_value=None)
    cache.stream_tail = AsyncMock(return_value=None)
    for k, v in overrides.items():
        setattr(cache, k, v)
    return cache


async def _append(cache, **kwargs):
    await stream_append_with_retry(
        cache,
        "workflow:stream:t1:r1",
        event_id=kwargs.pop("event_id", 5),
        max_size=1000,
        stream_event=FRAME,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_happy_path_writes_once():
    cache = _cache()
    await _append(cache)
    assert cache.pipelined_event_buffer.await_count == 1
    cache.stream_tail.assert_not_awaited()


@pytest.mark.asyncio
async def test_pool_exhaustion_replays_the_identical_write():
    """Nothing reached the server, so there is nothing to probe — and the
    epoch DEL is safe to repeat because it never ran."""
    cache = _cache()
    cache.pipelined_event_buffer = AsyncMock(
        side_effect=[
            redis_exceptions.ConnectionError("No connection available."),
            None,
        ]
    )
    await _append(cache, event_id=1)

    assert cache.pipelined_event_buffer.await_count == 2
    cache.stream_tail.assert_not_awaited()
    # Still not bare: nothing was sent, so the epoch DEL never ran and the
    # replay must carry it.
    assert cache.pipelined_event_buffer.await_args.kwargs["bare"] is False


@pytest.mark.asyncio
async def test_ambiguous_write_that_landed_is_accepted():
    cache = _cache(stream_tail=AsyncMock(return_value=(5, OURS)))
    cache.pipelined_event_buffer = AsyncMock(
        side_effect=redis_exceptions.TimeoutError("Timeout reading from redis")
    )

    await _append(cache, event_id=5)

    assert cache.pipelined_event_buffer.await_count == 1
    cache.stream_tail.assert_awaited_once()


@pytest.mark.asyncio
async def test_tail_past_our_id_is_fatal_never_success():
    """The I6 guard. An auto-id frame (a recovery-appended error, or run_end)
    puts the tail far above our id; treating the resulting duplicate rejection
    as success would let the run complete with this frame missing."""
    cache = _cache(stream_tail=AsyncMock(return_value=(1769000000000, b"run_end")))
    cache.pipelined_event_buffer = AsyncMock(
        side_effect=redis_exceptions.TimeoutError("Timeout reading from redis")
    )

    with pytest.raises(StreamAppendError, match="past"):
        await _append(cache, event_id=5)

    assert cache.pipelined_event_buffer.await_count == 1


@pytest.mark.asyncio
async def test_our_id_carrying_other_bytes_is_fatal_never_success():
    """The other I6 guard, and the reason the probe reads the payload at all.

    A run's stream is DELed at event 1, so a crashed predecessor's frame can
    sit under the very id being written. The id matches; the write never
    happened. Accepting it archives a frame this run did not write.
    """
    cache = _cache(stream_tail=AsyncMock(return_value=(1, b"a predecessor")))
    cache.pipelined_event_buffer = AsyncMock(
        side_effect=redis_exceptions.TimeoutError("Timeout reading from redis")
    )

    with pytest.raises(StreamAppendError, match="different frame"):
        await _append(cache, event_id=1)

    assert cache.pipelined_event_buffer.await_count == 1


@pytest.mark.asyncio
async def test_an_entry_without_our_field_is_not_read_as_ours():
    """A foreign writer need not carry the ``event`` field; a missing payload
    must read as "not ours", never as a vacuous match."""
    cache = _cache(stream_tail=AsyncMock(return_value=(5, None)))
    cache.pipelined_event_buffer = AsyncMock(
        side_effect=redis_exceptions.TimeoutError("Timeout reading from redis")
    )

    with pytest.raises(StreamAppendError, match="different frame"):
        await _append(cache, event_id=5)


@pytest.mark.asyncio
async def test_ambiguous_write_that_did_not_land_is_retried_bare():
    """The heal PERSIST is dropped on retry: re-running it could re-immortalize
    a stream something else already stamped terminal."""
    cache = _cache(stream_tail=AsyncMock(return_value=(4, OURS)))
    cache.pipelined_event_buffer = AsyncMock(
        side_effect=[
            redis_exceptions.TimeoutError("Timeout reading from redis"),
            None,
        ]
    )

    await _append(cache, event_id=5)

    assert cache.pipelined_event_buffer.await_count == 2
    first, second = cache.pipelined_event_buffer.await_args_list
    assert first.kwargs["bare"] is False
    assert second.kwargs["bare"] is True


@pytest.mark.asyncio
async def test_ambiguous_epoch_reset_retries_without_repeating_the_del():
    """Repeating the DEL could erase a frame this process never wrote. Dropping
    it costs nothing: if it was needed but never ran, the leftover tail rejects
    our id instead of being silently overwritten."""
    cache = _cache(stream_tail=AsyncMock(return_value=None))
    cache.pipelined_event_buffer = AsyncMock(
        side_effect=[
            redis_exceptions.TimeoutError("Timeout reading from redis"),
            None,
        ]
    )

    await _append(cache, event_id=1)

    assert cache.pipelined_event_buffer.await_count == 2
    first, second = cache.pipelined_event_buffer.await_args_list
    assert first.kwargs["bare"] is False
    assert second.kwargs["bare"] is True


@pytest.mark.asyncio
async def test_duplicate_id_on_a_first_attempt_is_fatal():
    """Nothing ambiguous has happened yet, so a rejected id means the stream
    already holds state this run did not write — not a lost reply."""
    cache = _cache(stream_tail=AsyncMock(return_value=(5, OURS)))
    cache.pipelined_event_buffer = AsyncMock(
        side_effect=redis_exceptions.ResponseError(
            "The ID specified in XADD is equal or smaller than the target "
            "stream top item"
        )
    )

    with pytest.raises(StreamAppendError, match="first attempt"):
        await _append(cache, event_id=5)

    cache.stream_tail.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_id_after_an_ambiguous_attempt_reads_as_landed():
    cache = _cache(stream_tail=AsyncMock(side_effect=[None, (5, OURS)]))
    cache.pipelined_event_buffer = AsyncMock(
        side_effect=[
            redis_exceptions.TimeoutError("Timeout reading from redis"),
            redis_exceptions.ResponseError(
                "The ID specified in XADD is equal or smaller than the target "
                "stream top item"
            ),
        ]
    )

    await _append(cache, event_id=5)

    assert cache.pipelined_event_buffer.await_count == 2


@pytest.mark.asyncio
async def test_failed_probe_reads_as_unknown_and_retries():
    """A probe that itself errors must not be read as "the stream is empty" —
    retrying the bare XADD is safe either way, the fence decides."""
    cache = _cache(
        stream_tail=AsyncMock(side_effect=redis_exceptions.RedisError("down"))
    )
    cache.pipelined_event_buffer = AsyncMock(
        side_effect=[
            redis_exceptions.TimeoutError("Timeout reading from redis"),
            None,
        ]
    )

    await _append(cache, event_id=5)
    assert cache.pipelined_event_buffer.await_count == 2


@pytest.mark.asyncio
async def test_unavailable_transport_is_not_retried():
    """Nothing to wait for — burning the retry budget only delays the failure."""
    cache = _cache()
    cache.pipelined_event_buffer = AsyncMock(
        side_effect=EventBufferUnavailableError("disabled")
    )

    with pytest.raises(StreamAppendError):
        await _append(cache)

    assert cache.pipelined_event_buffer.await_count == 1
