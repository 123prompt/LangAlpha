"""Tests for the user MCP catalog router (app/mcp_catalog.py).

Covers list/get/create/update/delete, 409 on duplicate, 404 on missing, the
name-mismatch guard on PUT, and that env/header literal values are masked in
responses (only vault refs surfaced).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_test_app


def _row(name="remote_server", **overrides):
    base = {
        "user_mcp_server_id": "11111111-1111-1111-1111-111111111111",
        "user_id": "test-user-123",
        "name": name,
        "transport": "http",
        "command": None,
        "args": [],
        "url": "https://api.example.com/mcp",
        "env": {},
        "headers": {"Authorization": "${vault:API_KEY}", "X-Trace": "literal-value"},
        "description": "d",
        "instruction": "i",
        "tool_exposure_mode": "summary",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(overrides)
    return base


@pytest_asyncio.fixture
async def client():
    from src.server.app.mcp_catalog import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_list_masks_literals_and_reports_max(client):
    with patch(
        "src.server.app.mcp_catalog.list_catalog_servers",
        new=AsyncMock(return_value=[_row()]),
    ):
        resp = await client.get("/api/v1/mcp/servers")
    assert resp.status_code == 200
    body = resp.json()
    assert "literal-value" not in resp.text
    assert body["servers"][0]["header_refs"] == ["API_KEY"]
    assert body["max_servers"] == 50


@pytest.mark.asyncio
async def test_list_reports_hash_gated_tool_counts(client):
    """tool_count mirrors the workspace rule: only an ok snapshot discovered
    under the server's CURRENT fingerprint counts; stale/error/missing ⇒ null
    (never 0 — the UI hides null, and 0 would claim a discovery that isn't
    current)."""
    from src.server.services.mcp_config import user_row_to_server_config
    from src.server.services.mcp_discovery import mcp_discovery_fingerprint

    rows = [_row(), _row(name="stale_server"), _row(name="never_discovered")]
    current_fp = mcp_discovery_fingerprint(user_row_to_server_config(rows[0]))
    schemas = [
        {"server_name": "remote_server", "status": "ok", "error": "",
         "config_hash": current_fp,
         "tools": [{"name": "a"}, {"name": "b"}]},
        {"server_name": "stale_server", "status": "ok", "error": "",
         "config_hash": "not-the-current-fingerprint",
         "tools": [{"name": "a"}]},
    ]
    with (
        patch(
            "src.server.app.mcp_catalog.list_catalog_servers",
            new=AsyncMock(return_value=rows),
        ),
        patch(
            "src.server.app.mcp_catalog.get_user_tool_schemas",
            new=AsyncMock(return_value=schemas),
        ),
    ):
        resp = await client.get("/api/v1/mcp/servers")
    assert resp.status_code == 200
    by_name = {s["name"]: s for s in resp.json()["servers"]}
    assert by_name["remote_server"]["tool_count"] == 2
    assert by_name["stale_server"]["tool_count"] is None
    assert by_name["never_discovered"]["tool_count"] is None


@pytest.mark.asyncio
async def test_create_happy(client):
    with patch(
        "src.server.app.mcp_catalog.create_catalog_server",
        new=AsyncMock(return_value=_row(name="new_server")),
    ):
        resp = await client.post(
            "/api/v1/mcp/servers",
            json={
                "name": "new_server",
                "transport": "http",
                "url": "https://api.example.com/mcp",
                "headers": {"Authorization": "${vault:API_KEY}"},
            },
        )
    assert resp.status_code == 201
    assert resp.json()["name"] == "new_server"


@pytest.mark.asyncio
async def test_create_duplicate_409(client):
    with patch(
        "src.server.app.mcp_catalog.create_catalog_server",
        new=AsyncMock(side_effect=ValueError("already exists")),
    ):
        resp = await client.post(
            "/api/v1/mcp/servers",
            json={"name": "dup", "transport": "stdio", "command": "npx"},
        )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_rejects_bash(client):
    resp = await client.post(
        "/api/v1/mcp/servers",
        json={"name": "evil", "transport": "stdio", "command": "bash"},
    )
    assert resp.status_code == 422
    # 422 detail is a flat string, not FastAPI's default list shape.
    assert isinstance(resp.json()["detail"], str)


@pytest.mark.asyncio
async def test_create_over_cap_409(client):
    with patch(
        "src.server.app.mcp_catalog.create_catalog_server",
        new=AsyncMock(
            side_effect=ValueError(
                "Maximum of 50 MCP catalog servers per user reached"
            )
        ),
    ):
        resp = await client.post(
            "/api/v1/mcp/servers",
            json={"name": "over_cap", "transport": "stdio", "command": "npx"},
        )
    assert resp.status_code == 409
    assert "Maximum of 50" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_update_invalid_body_422_string_detail(client):
    resp = await client.put(
        "/api/v1/mcp/servers/remote_server",
        json={"name": "remote_server", "transport": "stdio", "command": "bash"},
    )
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)


@pytest.mark.asyncio
async def test_get_404(client):
    with patch(
        "src.server.app.mcp_catalog.get_catalog_server",
        new=AsyncMock(return_value=None),
    ):
        resp = await client.get("/api/v1/mcp/servers/ghost")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_name_mismatch_409(client):
    resp = await client.put(
        "/api/v1/mcp/servers/remote_server",
        json={"name": "different", "transport": "stdio", "command": "npx"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_update_missing_404(client):
    with patch(
        "src.server.app.mcp_catalog.update_catalog_server",
        new=AsyncMock(return_value=None),
    ):
        resp = await client.put(
            "/api/v1/mcp/servers/remote_server",
            json={"name": "remote_server", "transport": "stdio", "command": "npx"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_happy_and_404(client):
    # Delete must revoke any OAuth connection + its grants (no catalog FK), so
    # the handler routes through disconnect_server before dropping the row.
    disconnect = AsyncMock(return_value=True)
    with (
        patch(
            "src.server.services.mcp_oauth.lifecycle.disconnect_server",
            new=disconnect,
        ),
        patch(
            "src.server.app.mcp_catalog.delete_catalog_server",
            new=AsyncMock(return_value=True),
        ),
    ):
        ok = await client.delete("/api/v1/mcp/servers/remote_server")
    assert ok.status_code == 200 and ok.json() == {"ok": True}
    disconnect.assert_awaited_once_with("test-user-123", "remote_server")

    with (
        patch(
            "src.server.services.mcp_oauth.lifecycle.disconnect_server",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "src.server.app.mcp_catalog.delete_catalog_server",
            new=AsyncMock(return_value=False),
        ),
    ):
        missing = await client.delete("/api/v1/mcp/servers/ghost")
    assert missing.status_code == 404
