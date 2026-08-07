"""Host-side tool discovery for OAuth-connected user servers.

OAuth servers are never probed from a sandbox (no token exists there); a
short-lived SDK session runs here instead — on connect, on manual refresh,
and from the sweeper when the snapshot ages out. The cache row lives in
``user_mcp_tool_schemas``; a schema-digest change fans out a version bump so
sessions re-resolve, while an unchanged re-discovery stays silent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from src.server.database.mcp_oauth import get_connection
from src.server.database.mcp_servers import (
    bump_user_workspaces_mcp_version,
    get_catalog_server,
    get_user_tool_schemas,
    upsert_user_tool_schemas,
)
from src.server.services.mcp_oauth.lifecycle import (
    TokenUnavailable,
    ensure_fresh_access_token,
)

logger = logging.getLogger(__name__)

DISCOVERY_TIMEOUT_S = 30

# Snapshots older than this are re-discovered by the sweeper: sandbox
# reconnects and ignored notifications can't invalidate a host-side cache, so
# age is the backstop.
SCHEMA_MAX_AGE_SECONDS = 6 * 3600


class _StreamsTransport:
    """Adapter: the stream context manager pair as a Client transport."""

    def __init__(self, streams_cm):
        self._cm = streams_cm

    async def __aenter__(self):
        return await self._cm.__aenter__()

    async def __aexit__(self, *exc):
        return await self._cm.__aexit__(*exc)


def _schema_digest(tools: list[dict]) -> str:
    canonical = json.dumps(tools, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def refresh_user_tool_schemas(user_id: str, server_name: str) -> dict:
    """Discover an OAuth server's tools host-side and cache the snapshot.

    Returns the cache row. Never raises for discovery failures — they land as
    an ``error`` row (the no-downgrade upsert keeps the last good snapshot).
    """
    from src.server.services.mcp_config import user_row_to_server_config
    from src.server.services.mcp_discovery import (
        mcp_discovery_fingerprint,
        sanitize_discovered_tools,
    )
    from src.server.utils.egress_guard import resolve_public_ips
    from urllib.parse import urlparse

    row = await get_catalog_server(user_id, server_name)
    if row is None:
        raise TokenUnavailable("unknown_server")
    connection = await get_connection(user_id, server_name)
    if connection is None or connection["status"] == "revoked":
        raise TokenUnavailable("unknown_connection")

    server = user_row_to_server_config(
        row, oauth_connection_id=connection["connection_id"]
    )
    fingerprint = mcp_discovery_fingerprint(server)

    async def _fail(error: str) -> dict:
        return await upsert_user_tool_schemas(
            user_id, server_name, fingerprint, status="error", error=error
        )

    try:
        token = await ensure_fresh_access_token(connection["connection_id"])
    except TokenUnavailable as e:
        return await _fail(f"token unavailable: {e.reason}")

    url = row["url"]
    parsed = urlparse(url)
    try:
        # Pre-connect SSRF check. The SDK session then dials the hostname
        # itself (TLS against the real name); the rebinding residual between
        # check and connect is accepted for this authenticated, read-only hop
        # — the load-bearing pinning is on the token/DCR hops and the relay.
        await resolve_public_ips(parsed.hostname or "", port=parsed.port or 443)
    except Exception as e:
        return await _fail(f"blocked url: {e}")

    headers = {
        "Authorization": f"{token['token_type']} {token['access_token']}"
    }
    try:
        async with asyncio.timeout(DISCOVERY_TIMEOUT_S):
            async with create_mcp_http_client(headers=headers) as http_client:
                transport = _StreamsTransport(
                    streamable_http_client(url, http_client=http_client)
                )
                async with Client(transport) as client:
                    result = await client.list_tools(cache_mode="refresh")
    except Exception as e:
        logger.warning(
            "[mcp_oauth] discovery failed for %s: %s", server_name, e
        )
        return await _fail(f"discovery failed: {e}")

    raw_tools = [
        {
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.input_schema or {},
        }
        for t in result.tools
    ]
    kept, skipped = sanitize_discovered_tools(raw_tools)
    for name, reason in skipped:
        logger.info(
            "[mcp_oauth] skipped tool %r on %s: %s", name, server_name, reason
        )

    digest = _schema_digest(kept)
    previous = {
        r["config_hash"]: r for r in await get_user_tool_schemas(user_id)
        if r["server_name"] == server_name
    }
    prior = previous.get(fingerprint)
    cached = await upsert_user_tool_schemas(
        user_id,
        server_name,
        fingerprint,
        tools=kept,
        status="ok",
        schema_digest=digest,
        observed_meta={"skipped": [list(s) for s in skipped]},
    )
    if prior is None or prior.get("schema_digest") != digest:
        # Tool surface changed → sessions must regenerate wrappers.
        await bump_user_workspaces_mcp_version(user_id)
    logger.info(
        "[mcp_oauth] discovered %d tools on %s (digest %s)",
        len(kept), server_name, digest[:12],
    )
    return cached
