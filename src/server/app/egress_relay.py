"""Sandbox egress relay — the only path from a sandbox to an OAuth vendor.

POST /v1/egress/{grant_id}: the sandbox's generated MCP client dials this
route instead of the vendor; the relay authenticates the sandbox (relay JWT —
NEVER the app's user auth, which would let any logged-in browser drive
grants), attaches the vendor bearer host-side, and streams the exchange
through. No vendor token ever exists inside a sandbox in any form.

Deliberately outside the /api namespace: a machine endpoint, with clean
URLs on a dedicated API host (api.example.com/v1/egress/...).

Ships in OSS as an ordinary route: with no EGRESS_RELAY_SECRET configured it
answers 503 and is inert.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from src.server.services.egress.limits import RelayLimited, acquire_slot
from src.server.services.egress.relay import (
    WALL_CLOCK_S,
    RelayRejection,
    open_upstream,
    prepare_relay,
    sandbox_response_headers,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Egress Relay"])


def _reject(e: RelayRejection) -> Response:
    return Response(
        status_code=e.status,
        content=e.detail,
        media_type="text/plain",
        headers={"X-Relay-Error": e.code},
    )


@router.post("/v1/egress/{grant_id}")
async def relay(grant_id: str, request: Request) -> Response:
    raw_body = await request.body()
    try:
        prepared = await prepare_relay(
            grant_id,
            authorization=request.headers.get("authorization"),
            raw_body=raw_body,
        )
    except RelayRejection as e:
        return _reject(e)

    # One wall-clock budget covers token-to-last-byte; the concurrency slot is
    # held for the same span, so a slow vendor can pin a slot for at most
    # WALL_CLOCK_S, never minutes.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WALL_CLOCK_S

    resources = AsyncExitStack()
    try:
        async with asyncio.timeout_at(deadline):
            await resources.enter_async_context(
                acquire_slot(grant_id, prepared.grant["rate_class"])
            )
            upstream = await open_upstream(prepared, dict(request.headers))
    except RelayRejection as e:
        await resources.aclose()
        return _reject(e)
    except RelayLimited as e:
        await resources.aclose()
        return Response(
            status_code=429,
            content=f"relay limit: {e.kind}",
            media_type="text/plain",
            headers={"X-Relay-Error": f"limited_{e.kind}", "Retry-After": "5"},
        )
    except TimeoutError:
        await resources.aclose()
        return Response(
            status_code=504,
            content="relay wall clock exceeded",
            media_type="text/plain",
            headers={"X-Relay-Error": "wall_clock"},
        )
    except BaseException:
        await resources.aclose()
        raise

    async def stream() -> AsyncIterator[bytes]:
        # The vendor stream lives INSIDE the generator: starlette runs this
        # (and the finally) whether the exchange completes, the wall clock
        # fires, or the sandbox disconnects mid-stream.
        try:
            aiter = upstream.aiter_bytes()
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    logger.warning(
                        "[egress_relay] wall clock cut stream for grant %s",
                        grant_id,
                    )
                    break
                try:
                    chunk = await asyncio.wait_for(
                        aiter.__anext__(), timeout=remaining
                    )
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    logger.warning(
                        "[egress_relay] wall clock cut stream for grant %s",
                        grant_id,
                    )
                    break
                yield chunk
        finally:
            await upstream.aclose()
            await resources.aclose()

    return StreamingResponse(
        stream(),
        status_code=upstream.status_code,
        headers=sandbox_response_headers(upstream),
    )
