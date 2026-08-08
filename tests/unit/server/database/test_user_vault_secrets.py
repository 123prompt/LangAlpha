"""User-tier vault CRUD and the tier descriptor that parameterizes its SQL.

Both tiers share one set of statements, so the table/column names ARE f-string
interpolated. `_VaultTier`'s allowlist is the compensating control for that,
and these tests pin it alongside the user tier's own behavior.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

import src.server.database.user_vault_secrets as uvs
from src.server.database.vault_secrets import _VaultTier


@pytest.fixture
def mock_cursor():
    cursor = AsyncMock()
    cursor.execute = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=[])
    cursor.fetchone = AsyncMock(return_value=None)
    return cursor


@pytest.fixture
def vault_mock_db(mock_cursor):
    conn = AsyncMock()

    @asynccontextmanager
    async def _cursor_cm(**kwargs):
        yield mock_cursor

    conn.cursor = _cursor_cm

    @asynccontextmanager
    async def _fake_connection():
        yield conn

    # The user tier's SQL runs inside vault_secrets — that is where the pool
    # handle lives after the tier collapse.
    with patch(
        "src.server.database.vault_secrets.get_db_connection",
        new=_fake_connection,
    ):
        with patch(
            "src.server.database.vault_secrets._get_encryption_key",
            return_value="test-key",
        ):
            yield mock_cursor


# ---------------------------------------------------------------------------
# reveal_user_secret
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reveal_user_secret_returns_the_value(vault_mock_db):
    vault_mock_db.fetchone.return_value = {"plaintext": "sk-test-value"}

    assert await uvs.reveal_user_secret("user-1", "API_KEY") == "sk-test-value"

    sql, params = vault_mock_db.execute.call_args.args
    assert "user_vault_secrets" in sql
    assert params == ("test-key", "user-1", "API_KEY")


@pytest.mark.asyncio
async def test_reveal_user_secret_missing_returns_none(vault_mock_db):
    vault_mock_db.fetchone.return_value = None
    assert await uvs.reveal_user_secret("user-1", "NOPE") is None


@pytest.mark.asyncio
async def test_reveal_user_secret_does_not_decrypt_the_whole_vault(
    vault_mock_db, monkeypatch
):
    """The single-row read is the point: the old path decrypted every secret."""
    whole_vault = AsyncMock()
    monkeypatch.setattr(uvs, "get_user_secrets_decrypted", whole_vault)
    vault_mock_db.fetchone.return_value = {"plaintext": "v"}

    await uvs.reveal_user_secret("user-1", "API_KEY")

    whole_vault.assert_not_awaited()
    # _decrypted/_list are the fetchall-shaped reads; a scoped reveal uses none.
    vault_mock_db.fetchall.assert_not_awaited()
    sql = vault_mock_db.execute.call_args.args[0]
    assert "name = %s" in sql


# ---------------------------------------------------------------------------
# Tier delegation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_tier_queries_the_user_table(vault_mock_db):
    vault_mock_db.fetchall.return_value = [{"name": "A"}, {"name": "B"}]

    assert await uvs.get_user_secret_names("user-7") == {"A", "B"}

    sql, params = vault_mock_db.execute.call_args.args
    assert "user_vault_secrets" in sql
    assert "workspace" not in sql
    assert params == ("user-7",)


@pytest.mark.asyncio
async def test_delete_user_secret_reports_missing_rows(vault_mock_db):
    vault_mock_db.rowcount = 0
    assert await uvs.delete_user_secret("user-1", "GONE") is False

    vault_mock_db.rowcount = 1
    assert await uvs.delete_user_secret("user-1", "THERE") is True


# ---------------------------------------------------------------------------
# _VaultTier allowlist
# ---------------------------------------------------------------------------


def _tier(**overrides):
    kwargs = {
        "table": "user_vault_secrets",
        "owner_col": "user_id",
        "id_col": "user_vault_secret_id",
        "max_secrets": 5,
        "label": "user",
        "log_prefix": "[test]",
    }
    kwargs.update(overrides)
    return _VaultTier(**kwargs)


def test_tier_rejects_table_outside_allowlist():
    with pytest.raises(ValueError, match="Unknown vault table"):
        _tier(table="users")


def test_tier_rejects_injected_table_name():
    with pytest.raises(ValueError, match="Unknown vault table"):
        _tier(table="user_vault_secrets; DROP TABLE users --")


def test_tier_rejects_column_outside_allowlist():
    with pytest.raises(ValueError, match="Unknown vault column"):
        _tier(owner_col="user_id = '' OR 1=1 --")


def test_tier_rejects_injected_id_column():
    with pytest.raises(ValueError, match="Unknown vault column"):
        _tier(id_col="value")


def test_shipped_tiers_are_valid():
    from src.server.database.vault_secrets import WORKSPACE_TIER

    assert uvs.USER_TIER.table == "user_vault_secrets"
    assert uvs.USER_TIER.max_secrets == uvs.MAX_SECRETS_PER_USER
    assert WORKSPACE_TIER.table != uvs.USER_TIER.table
