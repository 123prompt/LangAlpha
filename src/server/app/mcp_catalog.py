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
from src.server.models.mcp_server import (
    CatalogServer,
    CatalogServerList,
    EnabledInput,
    McpServerInput,
    _format_validation_error,
    catalog_row_to_response,
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
    return catalog_row_to_response(row)


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
    return catalog_row_to_response(row)


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
    return {"name": name, "enabled": body.enabled}


@router.delete("/servers/{name}")
@handle_api_exceptions("delete MCP catalog server", logger)
async def delete_server(name: str, user_id: CurrentUserId) -> dict:
    found = await delete_catalog_server(user_id, name)
    if not found:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return {"ok": True}
