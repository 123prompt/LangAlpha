"""User-level MCP server API — the Connectors backing store.

An ``enabled`` row is live config: ``resolve_mcp_config`` inherits it into
every one of the user's workspaces. Disabled rows are inert templates (the
legacy catalog behavior; ``from_template`` still copies them into a
workspace). All env/header literals are masked in responses; only
``${vault:NAME}`` reference names are surfaced.

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

from ptc_agent.core.mcp_sanitize import VAULT_REF_RE

from src.server.database.mcp_oauth import list_connections
from src.server.database.mcp_servers import (
    MAX_CATALOG_SERVERS_PER_USER,
    create_catalog_server,
    delete_catalog_server,
    get_catalog_server,
    list_catalog_servers,
    set_catalog_server_enabled,
    update_catalog_server,
)
from src.server.database.user_vault_secrets import (
    create_user_secret,
    delete_user_secret,
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
from src.server.services.mcp_import import (
    extract_literals_to_vault,
    rollback_import_secrets,
)
from src.server.utils.api import CurrentUserId, handle_api_exceptions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mcp", tags=["MCP Catalog"])


def _builtin_names() -> set[str]:
    """Names of the process-global built-in MCP servers (from agent_config)."""
    from src.server.app import setup

    if setup.agent_config is None:
        return set()
    return {s.name for s in setup.agent_config.mcp.servers}


async def _oauth_status_by_server(user_id: str) -> dict[str, str]:
    """server_name → connection status, for decorating catalog responses."""
    try:
        return {
            c["server_name"]: c["status"] for c in await list_connections(user_id)
        }
    except Exception:
        logger.warning(
            "[mcp_catalog] OAuth connection lookup failed for %s", user_id,
            exc_info=True,
        )
        return {}


@router.get("/servers")
@handle_api_exceptions("list MCP catalog servers", logger)
async def list_servers(user_id: CurrentUserId) -> CatalogServerList:
    rows = await list_catalog_servers(user_id)
    oauth = await _oauth_status_by_server(user_id)
    return CatalogServerList(
        servers=[
            catalog_row_to_response(r, oauth_status=oauth.get(r["name"]))
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
    if server.name in _builtin_names():
        raise HTTPException(
            status_code=409,
            detail=f"{server.name!r} collides with a built-in server name",
        )
    try:
        row = await create_catalog_server(
            user_id,
            server.name,
            transport=server.transport,
            command=server.command,
            args=server.args,
            url=server.url,
            env=server.env,
            headers=server.headers,
            description=server.description,
            instruction=server.instruction,
            tool_exposure_mode=server.tool_exposure_mode,
            discovery_uses_secrets=server.discovery_uses_secrets,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    response = catalog_row_to_response(row)
    response.warnings = isolation_warnings(server) or None
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
    row = await update_catalog_server(
        user_id,
        name,
        updates={
            "transport": server.transport,
            "command": server.command,
            "args": server.args,
            "url": server.url,
            "env": server.env,
            "headers": server.headers,
            "description": server.description,
            "instruction": server.instruction,
            "tool_exposure_mode": server.tool_exposure_mode,
            "discovery_uses_secrets": server.discovery_uses_secrets,
        },
    )
    if not row:
        raise HTTPException(status_code=404, detail="MCP server not found")
    response = catalog_row_to_response(row)
    response.warnings = isolation_warnings(server) or None
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

    async def create_secret(name: str, value: str, description: str) -> None:
        await create_user_secret(user_id, name, value, description)

    async def delete_secret(name: str) -> None:
        await delete_user_secret(user_id, name)

    builtins = _builtin_names()
    existing_names = {r["name"] for r in await list_catalog_servers(user_id)}
    used_secret_names = set(await get_user_secret_names(user_id))

    # value → ${vault:NAME}, so an identical token reused across servers is
    # stored once.
    allocated: dict[str, str] = {}
    secrets_created: list[str] = []
    seen_names: set[str] = set()
    results: list[dict] = []
    created_count = 0

    for entry in parsed:
        base = {
            "original_name": entry.original_name,
            "name": entry.name,
            "renamed": entry.renamed,
        }
        if entry.error:
            results.append({**base, "status": "invalid", "error": entry.error})
            continue
        if entry.name in builtins:
            results.append(
                {**base, "status": "skipped", "reason": "collides with a built-in server"}
            )
            continue
        if entry.name in seen_names or entry.name in existing_names:
            reason = (
                "duplicate name after normalization"
                if entry.name in seen_names
                else "already exists in your Connectors"
            )
            status = "skipped" if entry.name in seen_names else "exists"
            results.append({**base, "status": status, "reason": reason})
            continue
        if len(existing_names) + created_count >= MAX_CATALOG_SERVERS_PER_USER:
            results.append(
                {
                    **base,
                    "status": "error",
                    "error": f"Connectors server cap "
                    f"({MAX_CATALOG_SERVERS_PER_USER}) reached",
                }
            )
            continue

        seen_names.add(entry.name)
        config = dict(entry.config)
        try:
            made = await extract_literals_to_vault(
                entry.name,
                config,
                allocated=allocated,
                used_secret_names=used_secret_names,
                create_secret=create_secret,
                delete_secret=delete_secret,
            )
        except ValueError as e:
            results.append({**base, "status": "error", "error": str(e)})
            continue

        # An authenticated remote server needs its header even to list tools,
        # so discovery must resolve secrets — store that flag honestly.
        if config.get("transport") in ("http", "sse"):
            headers = config.get("headers") or {}
            if any(VAULT_REF_RE.search(str(v)) for v in headers.values()):
                config["discovery_uses_secrets"] = True

        try:
            server = McpServerInput(**config)
        except ValidationError as e:
            await rollback_import_secrets(
                made,
                allocated=allocated,
                used_secret_names=used_secret_names,
                delete_secret=delete_secret,
            )
            results.append(
                {**base, "status": "invalid", "error": _format_validation_error(e)}
            )
            continue

        try:
            await create_catalog_server(
                user_id,
                server.name,
                transport=server.transport,
                command=server.command,
                args=server.args,
                url=server.url,
                env=server.env,
                headers=server.headers,
                description=server.description,
                instruction=server.instruction,
                tool_exposure_mode=server.tool_exposure_mode,
                discovery_uses_secrets=server.discovery_uses_secrets,
            )
        except ValueError as e:
            await rollback_import_secrets(
                made,
                allocated=allocated,
                used_secret_names=used_secret_names,
                delete_secret=delete_secret,
            )
            results.append({**base, "status": "error", "error": str(e)})
            continue

        secrets_created.extend(made)
        created_count += 1
        results.append({**base, "status": "created"})

    # Imported rows are disabled (inert) — no fan-out, no sandbox push needed.
    return {
        "results": results,
        "created": created_count,
        "secrets_created": secrets_created,
        "config_version": 0,
    }


async def _relay_execution_warning(user_id: str, name: str) -> str | None:
    """OAuth-connected servers execute only via the egress relay — activation
    is the moment to tell the user their deployment can't actually run them."""
    from src.config.env import EGRESS_RELAY_SECRET
    from src.server.app import setup
    from src.server.database.mcp_oauth import get_connection
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
    return out


@router.delete("/servers/{name}")
@handle_api_exceptions("delete MCP catalog server", logger)
async def delete_server(name: str, user_id: CurrentUserId) -> dict:
    found = await delete_catalog_server(user_id, name)
    if not found:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return {"ok": True}
