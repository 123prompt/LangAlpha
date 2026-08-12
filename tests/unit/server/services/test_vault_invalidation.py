"""Vault-mutation → MCP cache invalidation, both tiers.

The discovery fingerprint hashes ``${vault:NAME}`` ref strings, never secret
values, so a value change alone can't churn any config hash. These tests pin
the explicit compensation: a secret change that touches a referencing server
bumps the config version, purges the discovery snapshots of secret-using
referencing servers, and schedules a proactive apply — and a change to an
un-referenced secret does none of that.

The tiers are values, so a test swaps the two DB writes and the workspace
fan-out with ``dataclasses.replace``; a separate test pins that the shipped
tiers point at the real functions.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.server.app.mcp_servers as mcp_servers_mod
import src.server.services.vault_invalidation as vi
from ptc_agent.config.core import MCPServerConfig
from src.server.services.vault_invalidation import USER_TIER, WORKSPACE_TIER, refs_for_server


def _ws_row(name: str, config: dict) -> dict:
    return {"name": name, "source": "workspace", "enabled": True, "config": config}


def _user_row(name: str = "svc", **overrides) -> dict:
    row = {
        "name": name,
        "transport": "stdio",
        "command": "npx",
        "args": [],
        "url": None,
        "env": {},
        "headers": {},
        "description": "",
        "instruction": "",
    }
    row.update(overrides)
    return row


@pytest.fixture
def probes(monkeypatch):
    """Swap both DB writes, the workspace fan-out, the sandbox push and the
    apply scheduler; return (purge_and_bump, bump, schedule)."""
    purge_bump = AsyncMock(return_value=1)
    bump = AsyncMock()
    sched = MagicMock()
    monkeypatch.setattr(mcp_servers_mod, "_schedule_proactive_apply", sched)
    monkeypatch.setattr(vi, "_push_secrets", AsyncMock())
    return purge_bump, bump, sched


def _tier(base, probes, workspaces=("ws-1",)):
    purge_bump, bump, _ = probes
    return dataclasses.replace(
        base,
        purge_and_bump=purge_bump,
        bump=bump,
        workspaces=AsyncMock(return_value=list(workspaces)),
    )


# ---------------------------------------------------------------------------
# refs_for_server
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"env": {"TOKEN": "${vault:API_KEY}"}},
        {"headers": {"Authorization": "Bearer ${vault:API_KEY}"}},
        {"args": ["--key", "${vault:API_KEY}"]},
        {"url": "https://example.com/mcp?k=${vault:API_KEY}"},
    ],
    ids=["env", "headers", "args", "url"],
)
def test_substituted_fields_are_scanned(kwargs):
    server = MCPServerConfig(name="svc", source="user", **kwargs)
    assert refs_for_server(server) == {"API_KEY"}


@pytest.mark.parametrize("field", ["description", "instruction"])
def test_free_text_fields_are_not_scanned(field):
    """These are never substituted, so a ref written there is just prose."""
    server = MCPServerConfig(
        name="svc", source="user", **{field: "use ${vault:API_KEY} here"}
    )
    assert refs_for_server(server) == set()


def test_collects_every_referenced_name():
    server = MCPServerConfig(
        name="svc",
        source="user",
        env={"A": "${vault:ONE}"},
        headers={"H": "${vault:TWO}"},
        args=["${vault:THREE}"],
        url="https://x/${vault:FOUR}",
    )
    assert refs_for_server(server) == {"ONE", "TWO", "THREE", "FOUR"}


# ---------------------------------------------------------------------------
# Workspace tier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secret_change_purges_and_bumps_for_secret_using_server(
    monkeypatch, probes
):
    purge_bump, bump, sched = probes
    rows = [
        # Remote server authenticating via the changed secret: its discovery
        # runs WITH secrets, so its cached tools/list may depend on the value.
        _ws_row("authy", {
            "transport": "http",
            "url": "https://api.example.com/mcp",
            "headers": {"Authorization": "${vault:API_KEY}"},
        }),
        # References a DIFFERENT secret — untouched.
        _ws_row("other", {
            "transport": "stdio",
            "command": "npx",
            "env": {"TOKEN": "${vault:OTHER_KEY}"},
        }),
    ]
    monkeypatch.setattr(vi, "list_workspace_servers", AsyncMock(return_value=rows))

    await vi.after_secret_change(
        _tier(WORKSPACE_TIER, probes), "ws-1", "API_KEY", user_id="user-1"
    )

    # Purge and version bump ride ONE atomic call; no separate bump.
    purge_bump.assert_awaited_once_with("ws-1", ["authy"])
    bump.assert_not_awaited()
    sched.assert_called_once_with("ws-1", "user-1")


@pytest.mark.asyncio
async def test_stdio_env_ref_bumps_without_purge(monkeypatch, probes):
    """A stdio server's discovery runs secret-less, so its snapshot can't
    depend on the value — no purge, but the bump still re-resolves the live
    session (covers the needs_secret → ready transition)."""
    purge_bump, bump, sched = probes
    rows = [
        _ws_row("plain", {
            "transport": "stdio",
            "command": "npx",
            "env": {"TOKEN": "${vault:API_KEY}"},
        }),
    ]
    monkeypatch.setattr(vi, "list_workspace_servers", AsyncMock(return_value=rows))

    await vi.after_secret_change(
        _tier(WORKSPACE_TIER, probes), "ws-1", "API_KEY", user_id="user-1"
    )

    purge_bump.assert_not_awaited()
    bump.assert_awaited_once_with("ws-1")
    sched.assert_called_once_with("ws-1", "user-1")


@pytest.mark.asyncio
async def test_unreferenced_secret_is_a_noop(monkeypatch, probes):
    purge_bump, bump, sched = probes
    rows = [
        _ws_row("authy", {
            "transport": "http",
            "url": "https://api.example.com/mcp",
            "headers": {"Authorization": "${vault:API_KEY}"},
        }),
    ]
    monkeypatch.setattr(vi, "list_workspace_servers", AsyncMock(return_value=rows))

    await vi.after_secret_change(
        _tier(WORKSPACE_TIER, probes), "ws-1", "UNRELATED", user_id="user-1"
    )

    purge_bump.assert_not_awaited()
    bump.assert_not_awaited()
    sched.assert_not_called()


@pytest.mark.asyncio
async def test_description_only_edit_skips_the_cache_half(monkeypatch, probes):
    purge_bump, bump, sched = probes
    servers = AsyncMock()
    monkeypatch.setattr(vi, "list_workspace_servers", servers)

    await vi.after_secret_change(
        _tier(WORKSPACE_TIER, probes), "ws-1", "API_KEY",
        user_id="user-1", value_changed=False,
    )

    servers.assert_not_awaited()
    purge_bump.assert_not_awaited()
    bump.assert_not_awaited()
    sched.assert_not_called()


@pytest.mark.asyncio
async def test_invalidation_failure_never_raises(monkeypatch, probes):
    """Best-effort: a DB failure during invalidation must not fail the vault
    mutation that triggered it."""
    monkeypatch.setattr(
        vi, "list_workspace_servers", AsyncMock(side_effect=RuntimeError("db down"))
    )

    await vi.after_secret_change(
        _tier(WORKSPACE_TIER, probes), "ws-1", "API_KEY", user_id="user-1"
    )  # no raise


# ---------------------------------------------------------------------------
# User tier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_secret_purges_user_snapshots_and_fans_out(monkeypatch, probes):
    """The user tier gets the same purge the workspace tier always had, and the
    proactive apply reaches every RUNNING workspace of the user."""
    purge_bump, bump, sched = probes
    rows = [
        _user_row(
            "authy",
            transport="http",
            command=None,
            url="https://api.example.com/mcp",
            headers={"Authorization": "${vault:API_KEY}"},
        ),
    ]
    monkeypatch.setattr(vi, "list_enabled_user_servers", AsyncMock(return_value=rows))

    await vi.after_secret_change(
        _tier(USER_TIER, probes, workspaces=("ws-1", "ws-2")),
        "user-1", "API_KEY", user_id="user-1",
    )

    purge_bump.assert_awaited_once_with("user-1", ["authy"])
    bump.assert_not_awaited()
    assert [c.args for c in sched.call_args_list] == [
        ("ws-1", "user-1"), ("ws-2", "user-1"),
    ]


@pytest.mark.asyncio
async def test_user_free_text_reference_does_not_bump(monkeypatch, probes):
    purge_bump, bump, sched = probes
    rows = [_user_row(description="set ${vault:API_KEY} first")]
    monkeypatch.setattr(vi, "list_enabled_user_servers", AsyncMock(return_value=rows))

    await vi.after_secret_change(
        _tier(USER_TIER, probes), "user-1", "API_KEY", user_id="user-1"
    )

    purge_bump.assert_not_awaited()
    bump.assert_not_awaited()
    sched.assert_not_called()


@pytest.mark.asyncio
async def test_user_stdio_ref_bumps_every_workspace(monkeypatch, probes):
    purge_bump, bump, _ = probes
    rows = [_user_row(env={"TOKEN": "${vault:API_KEY}"})]
    monkeypatch.setattr(vi, "list_enabled_user_servers", AsyncMock(return_value=rows))

    await vi.after_secret_change(
        _tier(USER_TIER, probes), "user-1", "API_KEY", user_id="user-1"
    )

    purge_bump.assert_not_awaited()
    bump.assert_awaited_once_with("user-1")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_shipped_tiers_write_their_own_tables():
    """The tests above swap the callables, so pin the real wiring here — a tier
    crossed over would invalidate the wrong owner's cache."""
    from src.server.database.mcp_servers import (
        bump_user_workspaces_mcp_version,
        bump_workspace_mcp_version,
    )
    from src.server.database.mcp_tool_schemas import (
        delete_tool_schemas_and_bump,
        delete_user_tool_schemas_and_bump,
    )
    from src.server.database.workspace import get_running_workspace_ids_for_user

    assert WORKSPACE_TIER.purge_and_bump is delete_tool_schemas_and_bump
    assert WORKSPACE_TIER.bump is bump_workspace_mcp_version
    assert USER_TIER.purge_and_bump is delete_user_tool_schemas_and_bump
    assert USER_TIER.bump is bump_user_workspaces_mcp_version
    assert USER_TIER.workspaces is get_running_workspace_ids_for_user


@pytest.mark.asyncio
async def test_workspace_tier_converges_only_itself():
    assert await WORKSPACE_TIER.workspaces("ws-9") == ["ws-9"]


@pytest.mark.asyncio
async def test_disable_markers_are_not_scanned(monkeypatch, probes):
    """Rows with no config (built-in disable markers) carry no refs."""
    purge_bump, bump, _ = probes
    rows = [{"name": "builtin", "source": "builtin", "enabled": False, "config": None}]
    monkeypatch.setattr(vi, "list_workspace_servers", AsyncMock(return_value=rows))

    await vi.after_secret_change(
        _tier(WORKSPACE_TIER, probes), "ws-1", "API_KEY", user_id="user-1"
    )

    purge_bump.assert_not_awaited()
    bump.assert_not_awaited()
