"""Ownership of the connection an egress grant is minted against.

A grant is what lets a sandbox spend someone's OAuth credential, so the only
contract worth pinning at this layer is that ``connection_id`` is *selected*
under the owner predicate rather than trusted from the caller: another user's
connection must produce no grant at all, indistinguishably from one that does
not exist.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import patch

import pytest

from src.server.database.egress_grants import (
    GRANT_KIND_OAUTH_MCP,
    GrantConnectionUnavailable,
    ensure_oauth_grant,
    retire_stale_grants,
)

OWNER = "user-owner"
INTRUDER = "user-intruder"
CONNECTION_ID = "11111111-1111-4111-8111-111111111111"
WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"


class _Cursor:
    """Models the one thing this SQL's correctness rests on: the INSERT rows
    come from a SELECT over the connections table, not from the parameters."""

    def __init__(self, connections: dict[str, str], grants: dict[tuple, str]) -> None:
        self._connections = connections  # connection_id -> owning user_id
        self._grants = grants  # (workspace, kind, connection) -> grant_id
        self._row: dict[str, Any] | None = None
        self.statements: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, params: tuple) -> None:
        self.statements.append((sql, params))
        user_id, workspace_id, kind, connection_id, owner = params
        # The source SELECT: no matching row ⇒ the INSERT inserts nothing and
        # ON CONFLICT never fires, so RETURNING yields nothing.
        if self._connections.get(connection_id) != owner:
            self._row = None
            return
        key = (workspace_id, kind, connection_id)
        self._grants.setdefault(key, f"grant-for-{connection_id}")
        self._row = {"grant_id": self._grants[key]}

    async def fetchone(self) -> dict[str, Any] | None:
        return self._row


@pytest.fixture
def db():
    """A connections table with exactly one connection, owned by OWNER."""
    cursor = _Cursor({CONNECTION_ID: OWNER}, {})

    @asynccontextmanager
    async def _cursor_cm(**kwargs):
        yield cursor

    class _Conn:
        cursor = staticmethod(_cursor_cm)

    @asynccontextmanager
    async def _fake_db():
        yield _Conn()

    with patch("src.server.database.egress_grants.get_db_connection", new=_fake_db):
        yield cursor


class TestOwnership:
    @pytest.mark.asyncio
    async def test_the_owner_gets_a_grant(self, db):
        grant_id = await ensure_oauth_grant(
            user_id=OWNER,
            workspace_id=WORKSPACE_ID,
            connection_id=CONNECTION_ID,
        )
        assert grant_id == f"grant-for-{CONNECTION_ID}"

    @pytest.mark.asyncio
    async def test_another_users_connection_yields_no_grant(self, db):
        """The id is real, but not theirs — it must bind into no workspace."""
        with pytest.raises(GrantConnectionUnavailable):
            await ensure_oauth_grant(
                user_id=INTRUDER,
                workspace_id=WORKSPACE_ID,
                connection_id=CONNECTION_ID,
            )

    @pytest.mark.asyncio
    async def test_an_unknown_connection_fails_the_same_way(self, db):
        """Same exception as the wrong-owner case: guessing ids teaches nothing."""
        with pytest.raises(GrantConnectionUnavailable):
            await ensure_oauth_grant(
                user_id=OWNER,
                workspace_id=WORKSPACE_ID,
                connection_id="33333333-3333-4333-8333-333333333333",
            )

    @pytest.mark.asyncio
    async def test_the_predicate_is_in_the_sql_not_the_caller(self, db):
        """Pinned structurally too — the fake can only model what the SQL says.

        Were the ownership filter to move out of the statement, every arm above
        would still pass against a differently-shaped fake.
        """
        await ensure_oauth_grant(
            user_id=OWNER,
            workspace_id=WORKSPACE_ID,
            connection_id=CONNECTION_ID,
        )
        sql, params = db.statements[0]
        flat = re.sub(r"\s+", " ", sql)
        assert "FROM user_mcp_oauth_connections c" in flat
        assert "WHERE c.connection_id = %s::uuid AND c.user_id = %s" in flat
        # The inserted connection_id AND destination_url both come from the
        # connection row (c.connection_id, c.server_url), never a parameter —
        # a caller can never steer the grant at a host the token wasn't issued
        # for. No destination_url parameter exists to pass.
        assert "SELECT %s, %s::uuid, %s, c.connection_id, c.server_url" in flat
        assert params[-2:] == (CONNECTION_ID, OWNER)


class TestIdempotence:
    @pytest.mark.asyncio
    async def test_re_ensuring_returns_the_same_grant(self, db):
        first = await ensure_oauth_grant(
            user_id=OWNER,
            workspace_id=WORKSPACE_ID,
            connection_id=CONNECTION_ID,
        )
        second = await ensure_oauth_grant(
            user_id=OWNER,
            workspace_id=WORKSPACE_ID,
            connection_id=CONNECTION_ID,
        )
        assert first == second
        assert db.statements[0][1][2] == GRANT_KIND_OAUTH_MCP


@pytest.fixture
def recorder():
    """Plain statement recorder (the ensure-shaped ``db`` fake unpacks the
    INSERT's five params and can't observe other statements)."""

    class _Recorder:
        def __init__(self) -> None:
            self.statements: list[tuple[str, tuple]] = []
            self.rowcount = 3

        async def execute(self, sql: str, params: tuple) -> None:
            self.statements.append((sql, params))

    cursor = _Recorder()

    @asynccontextmanager
    async def _cursor_cm(**kwargs):
        yield cursor

    class _Conn:
        cursor = staticmethod(_cursor_cm)

    @asynccontextmanager
    async def _conn_cm():
        yield _Conn()

    with patch(
        "src.server.database.egress_grants.get_db_connection", _conn_cm
    ):
        yield cursor


class TestRetireStaleGrants:
    """The retire predicate is what closes the authorization overhang: an
    active grant the resolved set no longer contains must stop being
    spendable, and the keep-list is the only thing that protects a grant."""

    @pytest.mark.asyncio
    async def test_retires_only_active_rows_outside_the_keep_list(self, recorder):
        retired = await retire_stale_grants(WORKSPACE_ID, keep_grant_ids=("g-keep",))
        assert retired == 3
        sql, params = recorder.statements[0]
        flat = re.sub(r"\s+", " ", sql)
        assert "SET status = 'revoked'" in flat
        assert (
            "WHERE workspace_id = %s AND kind = %s AND status = 'active'" in flat
        )
        assert "grant_id != ALL(%s::uuid[])" in flat
        assert params == (WORKSPACE_ID, GRANT_KIND_OAUTH_MCP, ["g-keep"])

    @pytest.mark.asyncio
    async def test_empty_keep_list_retires_everything_active(self, recorder):
        await retire_stale_grants(WORKSPACE_ID, keep_grant_ids=())
        _sql, params = recorder.statements[0]
        assert params[-1] == []
