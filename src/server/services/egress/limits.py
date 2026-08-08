"""Cross-worker rate + concurrency limits for the egress relay (Redis).

Limits are protective plumbing, not the security boundary (that's the JWT +
grant checks) — so an unreachable Redis fails OPEN with a warning rather than
taking every connector down with it.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from src.server.services.egress import RelayError

logger = logging.getLogger(__name__)

# Per-grant budgets, deliberately generous — a single agent turn fans out at
# most a handful of concurrent tool calls.
RATE_LIMIT_RPM = 120
CONCURRENCY_LIMIT = 4

# TTLs bound leak windows if a worker dies mid-request. The concurrency TTL
# must exceed the relay's 55s wall clock: a live request outliving its key
# would decr a fresh counter negative on release and hand out extra slots.
_RATE_KEY_TTL = 120
_CONC_KEY_TTL = 120


class RelayLimited(Exception):
    def __init__(self, kind: str):
        self.kind = kind  # "rate" | "concurrency"
        self.code = RelayError(f"limited_{kind}")
        super().__init__(kind)


@asynccontextmanager
async def acquire_slot(grant_id: str):
    """Hold one concurrency slot for the duration of a relayed request."""
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not (cache.enabled and cache.client):
        logger.warning("[egress_limits] Redis unavailable; limits fail open")
        yield
        return
    redis = cache.client

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
    if int(count) > RATE_LIMIT_RPM:
        raise RelayLimited("rate")

    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(conc_key)
            pipe.expire(conc_key, _CONC_KEY_TTL)
            inflight, _ = await pipe.execute()
    except Exception:
        logger.warning(
            "[egress_limits] concurrency check failed; failing open", exc_info=True
        )
        yield
        return

    try:
        if int(inflight) > CONCURRENCY_LIMIT:
            raise RelayLimited("concurrency")
        yield
    finally:
        try:
            await redis.decr(conc_key)
        except Exception:
            logger.warning(
                "[egress_limits] slot release failed for %s", grant_id
            )
