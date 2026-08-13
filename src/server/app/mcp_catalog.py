"""User-level MCP server API — the Connectors backing store.

An ``enabled`` row is live config: ``resolve_mcp_config`` inherits it into
every one of the user's workspaces. A disabled row is inert — a stored
definition that reaches no workspace until it is enabled. Every route here is
owner-scoped, so responses echo the stored env/header maps as written — vault
refs and the owner's own literals, never a resolved secret — because a PUT
replaces the whole row and the edit form has to round-trip them.
``env_refs``/``header_refs`` remain the display-only vault-name projection.

Endpoints (user-scoped):
- GET    /api/v1/mcp/servers
- POST   /api/v1/mcp/servers
- GET    /api/v1/mcp/servers/{name}
- PUT    /api/v1/mcp/servers/{name}
- PATCH  /api/v1/mcp/servers/{name}/enabled
- DELETE /api/v1/mcp/servers/{name}
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException
from pydantic import ValidationError

from src.server.database.egress_grants import revoke_grants_for_connection
from src.server.database.mcp_oauth import (
    ConnectionStatus,
    get_connection,
    list_connections,
)
from src.server.services.mcp_config import builtin_names
from src.server.database.mcp_servers import (
    MAX_CATALOG_SERVERS_PER_USER,
    create_catalog_server,
    delete_catalog_server,
    get_catalog_server,
    list_catalog_servers,
    set_catalog_server_enabled,
    update_catalog_server,
)
from src.server.database.mcp_tool_schemas import get_user_tool_schemas
from src.server.database.user_vault_secrets import (
    create_user_secret,
    get_user_secret_names,
)
from src.server.models.mcp_server import (
    CatalogServer,
    CatalogServerList,
    EnabledInput,
    McpServerInput,
    _format_validation_error,
    catalog_row_to_response,
    isolation_warnings,
    parse_mcp_servers_payload,
)
from src.server.services.mcp_import import ImportScope, run_mcp_import
from src.server.services.vault_invalidation import USER_TIER, after_secret_change
from src.server.utils.api import CurrentUserId, handle_api_exceptions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mcp", tags=["MCP Catalog"])


async def _oauth_status_by_server(user_id: str) -> dict[str, ConnectionStatus]:
    """server_name → connection status, for decorating catalog responses."""
    try:
        return {
            c["server_name"]: ConnectionStatus(c["status"])
            for c in await list_connections(user_id)
        }
    except Exception:
        logger.warning(
            "[mcp_catalog] OAuth connection lookup failed for %s", user_id,
            exc_info=True,
        )
        return {}


async def _tool_counts_by_server(
    user_id: str, rows: list[dict]
) -> dict[str, int]:
    """server_name → discovered tool count, hash-gated to the CURRENT config.

    Same acceptance rule as the workspace effective list (``ToolSnapshotIndex``
    owns it), so the number shown here always matches what workspaces serve.
    Pure decoration: any failure degrades to no counts, never a 500.
    """
    from src.server.services.mcp_config import user_row_to_server_config
    from src.server.services.mcp_discovery import ToolSnapshotIndex

    try:
        schema_rows = await get_user_tool_schemas(user_id)
    except Exception:
        logger.warning(
            "[mcp_catalog] tool-schema lookup failed for %s", user_id,
            exc_info=True,
        )
        return {}
    snapshots = ToolSnapshotIndex(user_rows=schema_rows)
    counts: dict[str, int] = {}
    for row in rows:
        try:
            snapshot = snapshots.ok(user_row_to_server_config(row))
        except Exception:  # noqa: BLE001 — malformed row: just omit the count
            continue
        if snapshot is not None:
            counts[row["name"]] = len(snapshot.get("tools") or [])
    return counts


async def _oauth_headers_warning(user_id: str, server: McpServerInput) -> str | None:
    """Warn when configured headers meet a live OAuth connection.

    The two are independently settable, but the OAuth path never sends the
    configured headers: the probe sends its own, host discovery and the relay
    send only the OAuth Authorization. Silence would read as pass-through.
    """
    if not server.headers:
        return None
    try:
        connection = await get_connection(user_id, server.name)
    except Exception:
        logger.warning(
            "[mcp_catalog] OAuth connection lookup failed for %s", user_id,
            exc_info=True,
        )
        return None
    if connection is None or connection.status == ConnectionStatus.REVOKED:
        return None
    return (
        "This server is OAuth-connected, so its configured headers are not "
        "sent: discovery and sandbox tool calls carry only the OAuth "
        "Authorization header. Disconnect OAuth to use headers instead."
    )


async def _write_warnings(user_id: str, server: McpServerInput) -> list[str] | None:
    """The write-time nudges for a catalog row: isolation, then dropped headers."""
    warnings = isolation_warnings(server)
    if headers_warning := await _oauth_headers_warning(user_id, server):
        warnings.append(headers_warning)
    return warnings or None


@router.get("/servers")
@handle_api_exceptions("list MCP catalog servers", logger)
async def list_servers(user_id: CurrentUserId) -> CatalogServerList:
    rows = await list_catalog_servers(user_id)
    oauth = await _oauth_status_by_server(user_id)
    tool_counts = await _tool_counts_by_server(user_id, rows)
    return CatalogServerList(
        servers=[
            catalog_row_to_response(
                r,
                oauth_status=oauth.get(r["name"]),
                tool_count=tool_counts.get(r["name"]),
            )
            for r in rows
        ],
        max_servers=MAX_CATALOG_SERVERS_PER_USER,
    )


@router.post("/servers", status_code=201)
@handle_api_exceptions("create MCP catalog server", logger)
async def create_server(
    user_id: CurrentUserId, body: dict = Body(...)
) -> CatalogServer:
    try:
        server = McpServerInput(**body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=_format_validation_error(e))
    if server.name in builtin_names():
        raise HTTPException(
            status_code=409,
            detail=f"{server.name!r} collides with a built-in server name",
        )
    try:
        row = await create_catalog_server(
            user_id, server.name, **server.to_catalog_fields()
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    response = catalog_row_to_response(row)
    # A brand-new name has no connection, but a recreate over a name whose
    # connection row outlived the old catalog entry does.
    response.warnings = await _write_warnings(user_id, server)
    return response


@router.get("/servers/{name}")
@handle_api_exceptions("get MCP catalog server", logger)
async def get_server(name: str, user_id: CurrentUserId) -> CatalogServer:
    row = await get_catalog_server(user_id, name)
    if not row:
        raise HTTPException(status_code=404, detail="MCP server not found")
    oauth = await _oauth_status_by_server(user_id)
    return catalog_row_to_response(row, oauth_status=oauth.get(name))


@router.put("/servers/{name}")
@handle_api_exceptions("update MCP catalog server", logger)
async def update_server(
    name: str, user_id: CurrentUserId, body: dict = Body(...)
) -> CatalogServer:
    try:
        server = McpServerInput(**body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=_format_validation_error(e))
    # The path name is authoritative; a renamed body is rejected to avoid
    # silently creating a second row under a different key.
    if server.name != name:
        raise HTTPException(
            status_code=409, detail="name in body must match the path name"
        )
    # Pre-update read: the rediscovery decision below needs the OLD fingerprint,
    # and the update returns only the new row.
    prior = await get_catalog_server(user_id, name)
    # The row write and its version fan-out are one transaction in the DB layer.
    row = await update_catalog_server(
        user_id, name, updates=server.to_catalog_fields()
    )
    if not row:
        raise HTTPException(status_code=404, detail="MCP server not found")

    # Force reconnect when the edit moves an OAuth-connected server off its
    # consented endpoint: the stored token was issued for the old host, so it
    # must not carry to the new one. The grant already pins to the connection's
    # server_url, so no token can leak in the meantime — this revokes the now-
    # stale connection so the UI shows a clean reconnect. The revoke writes only
    # OAuth state, never this catalog row, so the response is built from the row
    # we already hold.
    from src.server.services.mcp_oauth.discovery import (
        schedule_post_edit_rediscovery,
    )
    from src.server.services.mcp_oauth.lifecycle import revoke_if_consent_moved

    # The consent check runs against a fresh read, never this request's own
    # values: the write above and the revoke below are separate transactions,
    # so a concurrent edit that already moved the row back onto the consented
    # endpoint would otherwise be revoked on values no row still holds. A row
    # deleted underneath us needs neither — DELETE revokes on both sides of its
    # own drop.
    committed = await get_catalog_server(user_id, name)
    if committed is not None and not await revoke_if_consent_moved(
        user_id, name, transport=committed["transport"], url=committed.get("url")
    ):
        # Consent survived the edit; if the discovery fingerprint moved, the
        # connection's cached snapshot just went stale under it.
        schedule_post_edit_rediscovery(user_id, name, prior=prior, updated=row)
    response = catalog_row_to_response(row)
    # After the revoke above, so an edit that just severed the connection does
    # not warn about headers it has now made effective.
    response.warnings = await _write_warnings(user_id, server)
    return response


@router.post("/servers/import")
@handle_api_exceptions("import MCP catalog servers", logger)
async def import_servers(
    user_id: CurrentUserId, body: dict = Body(...)
) -> dict:
    """Parse a standard ``{"mcpServers": {...}}`` blob into the user catalog.

    Mirrors the workspace import (name coercion, transport mapping, literal
    credentials auto-extracted — here into the USER vault) with one deliberate
    difference: imported rows land ``enabled=false`` (inert templates), so an
    import never silently changes every workspace's toolset. The UI nudges the
    user to flip each one live.
    """
    parsed = parse_mcp_servers_payload(body)
    if not parsed:
        raise HTTPException(
            status_code=422,
            detail='No MCP servers found. Expected a JSON object like '
            '{"mcpServers": { "<name>": { ... } }}.',
        )

    async def create_secret(conn, secret) -> None:
        await create_user_secret(
            user_id, secret.name, secret.value, secret.description, conn=conn
        )

    async def persist(conn, server: McpServerInput) -> bool:
        # No ON CONFLICT arm here — a raced duplicate raises ValueError, so a
        # successful call always means "created".
        await create_catalog_server(
            user_id, server.name, conn=conn, **server.to_catalog_fields()
        )
        return True

    existing_names = {r["name"] for r in await list_catalog_servers(user_id)}
    report = await run_mcp_import(
        parsed,
        scope=ImportScope(
            reserved_names=builtin_names(),
            existing_names=existing_names,
            current_count=len(existing_names),
            cap=MAX_CATALOG_SERVERS_PER_USER,
            cap_message=(
                f"Connectors server cap "
                f"({MAX_CATALOG_SERVERS_PER_USER}) reached"
            ),
            exists_message="already exists in your Connectors",
            existing_secret_names=set(await get_user_secret_names(user_id)),
            create_secret=create_secret,
            persist=persist,
        ),
    )

    # The imported SERVERS land disabled (inert), so they need no fan-out; the
    # imported SECRETS do. One can complete a ``${vault:NAME}`` ref that an
    # already-enabled connector has been dangling on, and nothing else in this
    # path purges its snapshot, bumps the version, or pushes to a live sandbox.
    for name in dict.fromkeys(report.secrets_created):
        await after_secret_change(USER_TIER, user_id, name, user_id=user_id)

    return {
        "results": report.results,
        "created": report.created,
        "secrets_created": report.secrets_created,
        "config_version": 0,
    }


async def _relay_execution_warning(user_id: str, name: str) -> str | None:
    """OAuth-connected servers execute only via the egress relay — activation
    is the moment to tell the user their deployment can't actually run them."""
    from src.config.env import EGRESS_RELAY_SECRET
    from src.server.app import setup
    from src.server.services.egress.reachability import (
        effective_relay_base_url,
        relay_reachability_warning,
    )

    if setup.agent_config is None:
        return None
    if await get_connection(user_id, name) is None:
        return None
    if not EGRESS_RELAY_SECRET:
        return (
            "The egress relay is disabled (EGRESS_RELAY_SECRET is not set), so "
            "this server's tools cannot run in sandboxes. Set a strong "
            "EGRESS_RELAY_SECRET in the backend environment and restart."
        )
    provider = setup.agent_config.sandbox.provider
    return relay_reachability_warning(provider, effective_relay_base_url(provider))


@router.patch("/servers/{name}/enabled")
@handle_api_exceptions("toggle MCP catalog server", logger)
async def set_enabled(
    name: str, body: EnabledInput, user_id: CurrentUserId
) -> dict:
    """Flip a user server live/inert. The DB layer bumps every workspace's
    ``mcp_config_version`` in the same transaction (next-acquire convergence)."""
    found = await set_catalog_server_enabled(user_id, name, body.enabled)
    if not found:
        raise HTTPException(status_code=404, detail="MCP server not found")
    out: dict = {"name": name, "enabled": body.enabled}
    if body.enabled:
        warning = await _relay_execution_warning(user_id, name)
        if warning:
            out["warnings"] = [warning]
    else:
        # Disable must bite now, not at next acquire: an idle sandbox holds its
        # grant_id and a relay JWT for hours, and the relay checks grant and
        # connection status but never the catalog row. Safe against a
        # concurrent re-mint — the toggle's version bump above fails the
        # sync's CAS — and re-enable self-heals: the next acquire's grant sync
        # re-activates via its upsert arm.
        connection = await get_connection(user_id, name)
        if connection is not None:
            await revoke_grants_for_connection(connection.connection_id)
    return out


@router.delete("/servers/{name}")
@handle_api_exceptions("delete MCP catalog server", logger)
async def delete_server(name: str, user_id: CurrentUserId) -> dict:
    from src.server.services.mcp_oauth.lifecycle import disconnect_server

    # Revoke any OAuth connection + its grants before dropping the catalog row.
    # Deleting the row alone orphans the connection (no catalog FK): the refresh
    # sweeper keeps the token alive, and a same-name recreate silently reuses
    # it. disconnect_server is a no-op when no connection exists.
    await disconnect_server(user_id, name)
    found = await delete_catalog_server(user_id, name)
    if not found:
        raise HTTPException(status_code=404, detail="MCP server not found")
    # Fence the gap between those two writes — they are separate transactions,
    # so an OAuth callback landing in between leaves a connected row behind a
    # catalog entry that no longer exists: referenced by nothing, yet serviced
    # by the refresh sweeper forever (it has no catalog join). Revoking again
    # closes it, idempotently — no connection is a no-op, and revoked-on-revoked
    # is an accepted write.
    await disconnect_server(user_id, name)
    return {"ok": True}
