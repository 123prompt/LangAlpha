"""User-vault router: scoped vault-ref detection and the single-secret reveal.

The invalidation sweep must key off the fields that are actually substituted
at resolve time (env/headers/args/url) — scanning the whole row let a
``${vault:NAME}`` string in free-text description/instruction force a config
bump for every workspace of the user.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import src.server.app.user_vault as user_vault
from src.server.app.user_vault import _server_vault_refs


def _row(**overrides) -> dict:
    row = {
        "name": "svc",
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


# ---------------------------------------------------------------------------
# _server_vault_refs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row",
    [
        _row(env={"TOKEN": "${vault:API_KEY}"}),
        _row(headers={"Authorization": "Bearer ${vault:API_KEY}"}),
        _row(args=["--key", "${vault:API_KEY}"]),
        _row(url="https://example.com/mcp?k=${vault:API_KEY}"),
    ],
    ids=["env", "headers", "args", "url"],
)
def test_substituted_fields_are_scanned(row):
    assert _server_vault_refs(row) == {"API_KEY"}


@pytest.mark.parametrize("field", ["description", "instruction", "name"])
def test_free_text_fields_are_not_scanned(field):
    """These are never substituted, so a ref written there is just prose."""
    assert _server_vault_refs(_row(**{field: "use ${vault:API_KEY} here"})) == set()


def test_missing_and_null_fields_are_tolerated():
    assert _server_vault_refs({}) == set()
    assert _server_vault_refs(
        {"env": None, "headers": None, "args": None, "url": None}
    ) == set()


def test_collects_every_referenced_name():
    row = _row(
        env={"A": "${vault:ONE}"},
        headers={"H": "${vault:TWO}"},
        args=["${vault:THREE}"],
        url="https://x/${vault:FOUR}",
    )
    assert _server_vault_refs(row) == {"ONE", "TWO", "THREE", "FOUR"}


@pytest.mark.asyncio
async def test_bump_skipped_when_only_free_text_mentions_the_secret(monkeypatch):
    rows = [_row(description="set ${vault:API_KEY} first")]
    monkeypatch.setattr(
        user_vault, "list_enabled_user_servers", AsyncMock(return_value=rows)
    )
    bump = AsyncMock()
    monkeypatch.setattr(user_vault, "bump_user_workspaces_mcp_version", bump)
    monkeypatch.setattr(
        user_vault, "get_running_workspace_ids_for_user", AsyncMock(return_value=[])
    )

    await user_vault._after_mutation("user-1", "API_KEY", value_changed=True)

    bump.assert_not_awaited()


@pytest.mark.asyncio
async def test_bump_fires_when_a_substituted_field_references_the_secret(monkeypatch):
    rows = [_row(env={"TOKEN": "${vault:API_KEY}"})]
    monkeypatch.setattr(
        user_vault, "list_enabled_user_servers", AsyncMock(return_value=rows)
    )
    bump = AsyncMock()
    monkeypatch.setattr(user_vault, "bump_user_workspaces_mcp_version", bump)
    monkeypatch.setattr(
        user_vault, "get_running_workspace_ids_for_user", AsyncMock(return_value=[])
    )

    await user_vault._after_mutation("user-1", "API_KEY", value_changed=True)

    bump.assert_awaited_once_with("user-1")


# ---------------------------------------------------------------------------
# reveal endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reveal_endpoint_reads_one_secret(monkeypatch):
    reveal = AsyncMock(return_value="sk-test-value")
    monkeypatch.setattr(user_vault, "reveal_user_secret", reveal)

    assert await user_vault.reveal_secret("API_KEY", "user-1") == {
        "value": "sk-test-value"
    }
    reveal.assert_awaited_once_with("user-1", "API_KEY")


@pytest.mark.asyncio
async def test_reveal_endpoint_404s_on_missing(monkeypatch):
    monkeypatch.setattr(
        user_vault, "reveal_user_secret", AsyncMock(return_value=None)
    )

    with pytest.raises(HTTPException) as exc:
        await user_vault.reveal_secret("NOPE", "user-1")
    assert exc.value.status_code == 404


def test_router_no_longer_imports_the_whole_vault_decrypt():
    """The reveal path must not be able to fall back to a full-tier decrypt."""
    assert not hasattr(user_vault, "get_user_secrets_decrypted")
