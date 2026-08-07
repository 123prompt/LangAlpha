"""Cross-worker rate + concurrency limits for the egress relay (Redis).

Limits are protective plumbing, not the security boundary (that's the JWT +
grant checks) — so an unreachable Redis fails OPEN with a warning rather than
taking every connector down with it.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# Per-grant budgets by rate class. "default" is deliberately generous — a
# single agent turn fans out at most a handful of concurrent tool calls.
RATE_CLASSES: dict[str, dict[str, int]] = {
    "default": {"rpm": 120, "concurrency": 4},
}

# TTLs bound leak windows if a worker dies mid-request.
_RATE_KEY_TTL = 120
_CONC_KEY_TTL = 120


class RelayLimited(Exception):
    def __init__(self, kind: str):
        self.kind = kind  # "rate" | "concurrency"
        super().__init__(kind)


def _limits_for(rate_class: str) -> dict[str, int]:
    return RATE_CLASSES.get(rate_class) or RATE_CLASSES["default"]


@asynccontextmanager
async def acquire_slot(grant_id: str, rate_class: str):
    """Hold one concurrency slot for the duration of a relayed request."""
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not (cache.enabled and cache.client):
        logger.warning("[egress_limits] Redis unavailable; limits fail open")
        yield
        return
    redis = cache.client
    limits = _limits_for(rate_class)

    minute = int(time.time() // 60)
    rate_key = f"egress:rate:{grant_id}:{minute}"
    conc_key = f"egress:conc:{grant_id}"

    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(rate_key)
            pipe.expire(rate_key, _RATE_KEY_TTL)
            count, _ = await pipe.execute()
    except Exception:
        logger.warning("[egress_limits] rate check failed; failing open", exc_info=True)
        yield
        return
    if int(count) > limits["rpm"]:
        raise RelayLimited("rate")

    acquired = False
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(conc_key)
            pipe.expire(conc_key, _CONC_KEY_TTL)
            inflight, _ = await pipe.execute()
        acquired = True
        if int(inflight) > limits["concurrency"]:
            raise RelayLimited("concurrency")
        yield
    finally:
        if acquired:
            try:
                await redis.decr(conc_key)
            except Exception:
                logger.warning(
                    "[egress_limits] slot release failed for %s", grant_id
                )
