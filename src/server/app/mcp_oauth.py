"""User-level MCP OAuth API — connect, callback, disconnect, schema refresh.

Endpoints:
- POST   /api/v1/mcp/servers/{name}/oauth/start    → {authorize_url}
- GET    /api/v1/mcp/oauth/callback                → 302 back into the app
- DELETE /api/v1/mcp/servers/{name}/oauth          → disconnect
- POST   /api/v1/mcp/servers/{name}/oauth/refresh-schemas → host-side re-discovery

The callback carries NO session auth by design: it is the AS redirecting the
user's browser, on any worker — identity comes exclusively from the
single-use ``state`` record minted in phase 1.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import RedirectResponse

from src.server.services.mcp_oauth import (
    McpOAuthError,
    TokenUnavailable,
    complete_callback,
    disconnect_server,
    start_connect,
)
from src.server.utils.api import CurrentUserId, handle_api_exceptions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mcp", tags=["MCP OAuth"])


@router.post("/servers/{name}/oauth/start")
@handle_api_exceptions("start MCP OAuth connect", logger)
async def oauth_start(
    name: str, user_id: CurrentUserId, body: dict | None = Body(default=None)
) -> dict:
    return_to = (body or {}).get("return_to")
    try:
        result = await start_connect(user_id, name, return_to=return_to)
    except McpOAuthError as e:
        status = 404 if "not found" in str(e) else 422
        raise HTTPException(status_code=status, detail=str(e))
    return {"authorize_url": result["authorize_url"]}


@router.get("/oauth/callback")
async def oauth_callback(
    state: str | None = None,
    code: str | None = None,
    iss: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    """AS redirect target. Always answers a redirect — never an error page."""
    try:
        target = await complete_callback(
            state=state,
            code=code,
            iss=iss,
            error=error,
            error_description=error_description,
        )
    except Exception:
        logger.exception("[mcp_oauth] callback crashed")
        target = "/connectors?mcp_error=internal"
    # 303: the browser must GET the app route regardless of how it got here.
    return RedirectResponse(url=target, status_code=303)


@router.delete("/servers/{name}/oauth")
@handle_api_exceptions("disconnect MCP OAuth", logger)
async def oauth_disconnect(name: str, user_id: CurrentUserId) -> dict:
    found = await disconnect_server(user_id, name)
    if not found:
        raise HTTPException(status_code=404, detail="No OAuth connection found")
    return {"ok": True}


@router.post("/servers/{name}/oauth/refresh-schemas")
@handle_api_exceptions("refresh MCP OAuth schemas", logger)
async def oauth_refresh_schemas(name: str, user_id: CurrentUserId) -> dict:
    from src.server.services.mcp_oauth.discovery import refresh_user_tool_schemas

    try:
        row = await refresh_user_tool_schemas(user_id, name)
    except TokenUnavailable as e:
        raise HTTPException(
            status_code=409, detail=f"Connection unusable: {e.reason}"
        )
    return {
        "server_name": row["server_name"],
        "status": row["status"],
        "error": row.get("error") or "",
        "tool_count": len(row.get("tools") or []),
        "discovered_at": row.get("discovered_at"),
    }
