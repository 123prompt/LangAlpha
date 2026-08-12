"""Host-side tool discovery for OAuth-connected user servers.

OAuth servers are never probed from a sandbox (no token exists there); a
short-lived SDK session runs here instead — on connect and on manual refresh.
The cache row lives in ``user_mcp_tool_schemas``; a schema-digest change fans
out a version bump so sessions re-resolve, while an unchanged re-discovery
stays silent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from src.server.database.mcp_oauth import ConnectionStatus, get_connection
from src.server.database.mcp_servers import (
    bump_user_workspaces_mcp_version,
    get_catalog_server,
)
from src.server.database.mcp_tool_schemas import (
    get_user_tool_schemas,
    upsert_user_tool_schemas,
)
from src.server.services.mcp_oauth.lifecycle import (
    TokenUnavailable,
    ensure_fresh_access_token,
)

logger = logging.getLogger(__name__)

DISCOVERY_TIMEOUT_S = 30


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
        ToolSnapshotIndex,
        mcp_discovery_fingerprint,
        sanitize_discovered_tools,
    )
    from src.server.utils.egress_guard import resolve_public_ips
    from urllib.parse import urlparse

    row = await get_catalog_server(user_id, server_name)
    if row is None:
        raise TokenUnavailable("unknown_server")
    connection = await get_connection(user_id, server_name)
    if connection is None or connection.status == ConnectionStatus.REVOKED:
        raise TokenUnavailable("unknown_connection")

    server = user_row_to_server_config(
        row, oauth_connection_id=connection.connection_id
    )
    fingerprint = mcp_discovery_fingerprint(server)

    async def _fail(error: str) -> dict:
        return await upsert_user_tool_schemas(
            user_id, server_name, fingerprint, status="error", error=error
        )

    try:
        token = await ensure_fresh_access_token(connection.connection_id)
    except TokenUnavailable as e:
        return await _fail(f"token unavailable: {e.reason}")

    url = row["url"]
    parsed = urlparse(url)
    try:
        # Pre-connect SSRF check on the original hostname. The SDK session then
        # dials the hostname itself (TLS against the real name); the DNS-
        # rebinding residual between check and connect is accepted for this
        # authenticated, read-only hop — the load-bearing pinning is on the
        # token/DCR hops and the relay.
        await resolve_public_ips(parsed.hostname or "", port=parsed.port or 443)
    except Exception as e:
        return await _fail(f"blocked url: {e}")

    headers = {"Authorization": token.header()}
    try:
        async with asyncio.timeout(DISCOVERY_TIMEOUT_S):
            async with create_mcp_http_client(headers=headers) as http_client:
                # Refuse redirects: create_mcp_http_client defaults them ON, and
                # a redirect is the one hop the pre-check above can't cover — a
                # hostile server would 30x to an internal address (link-local
                # metadata, RFC1918) that never faced resolve_public_ips.
                http_client.follow_redirects = False
                # The streams context manager IS the SDK's Transport protocol.
                transport = streamable_http_client(url, http_client=http_client)
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
    # Same acceptance rule as every other consumer: only a snapshot taken under
    # this server's CURRENT fingerprint is comparable.
    prior = ToolSnapshotIndex(
        user_rows=await get_user_tool_schemas(user_id)
    ).snapshot(server)
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
