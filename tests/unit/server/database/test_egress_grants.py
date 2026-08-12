"""Ownership and atomicity of a workspace's egress grant set.

A grant is what lets a sandbox spend someone's OAuth credential, so two
contracts matter at this layer. ``connection_id`` is *selected* under the owner
predicate rather than trusted from the caller: another user's connection must
produce no grant at all, indistinguishably from one that does not exist. And
the upserts and the retirement of everything else commit together — a grant set
that committed without its retirement half is an authorization overhang the
sandbox can still spend.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from src.server.database.egress_grants import (
    GRANT_KIND_OAUTH_MCP,
    sync_oauth_grants,
)

OWNER = "user-owner"
INTRUDER = "user-intruder"
CONNECTION_ID = "11111111-1111-4111-8111-111111111111"
OTHER_CONNECTION_ID = "44444444-4444-4444-8444-444444444444"
UNKNOWN_CONNECTION_ID = "33333333-3333-4333-8333-333333333333"
WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"


class _Cursor:
    """Models the two things this SQL's correctness rests on: the INSERT rows
    come from a SELECT over the connections table (not from the parameters),
    and the retirement sweeps every active grant outside the keep list."""

    def __init__(self, connections: dict[str, str]) -> None:
        self._connections = connections  # connection_id -> owning user_id
        self.grants: dict[tuple, dict] = {}  # (workspace, kind, conn) -> row
        self._rows: list[dict] = []
        self.rowcount = 0
        self.depth = 0  # transaction nesting at the time of the last execute
        self.statements: list[tuple[str, tuple, int]] = []

    async def execute(self, sql: str, params: tuple) -> None:
        self.statements.append((sql, params, self.depth))
        if sql.lstrip().startswith("INSERT"):
            _user_id, workspace_id, kind, connection_ids, owner = params
            self._rows = []
            for connection_id in connection_ids:
                # The source SELECT: no matching row ⇒ nothing is inserted for
                # that id and ON CONFLICT never fires, so it never RETURNs.
                if self._connections.get(connection_id) != owner:
                    continue
                row = self.grants.setdefault(
                    (workspace_id, kind, connection_id),
                    {"grant_id": f"grant-for-{connection_id}", "status": "revoked"},
                )
                row["status"] = "active"
                self._rows.append(
                    {"connection_id": connection_id, "grant_id": row["grant_id"]}
                )
        else:
            workspace_id, kind, keep = params
            self._rows = []
            stale = [
                row
                for (ws, k, _c), row in self.grants.items()
                if ws == workspace_id
                and k == kind
                and row["status"] == "active"
                and row["grant_id"] not in keep
            ]
            for row in stale:
                row["status"] = "revoked"
            self.rowcount = len(stale)

    async def fetchall(self) -> list[dict]:
        return self._rows

    def active_grant_ids(self) -> set[str]:
        return {r["grant_id"] for r in self.grants.values() if r["status"] == "active"}


@pytest.fixture
def db():
    """A connections table with two connections, both owned by OWNER."""
    cursor = _Cursor({CONNECTION_ID: OWNER, OTHER_CONNECTION_ID: OWNER})

    @asynccontextmanager
    async def _cursor_cm(**kwargs):
        yield cursor

    @asynccontextmanager
    async def _transaction():
        cursor.depth += 1
        try:
            yield
        finally:
            cursor.depth -= 1

    class _Conn:
        cursor = staticmethod(_cursor_cm)
        transaction = staticmethod(_transaction)

    @asynccontextmanager
    async def _fake_db(conn=None):
        yield conn if conn is not None else _Conn()

    with patch("src.server.database.egress_grants.get_db_connection", new=_fake_db):
        yield cursor


async def _sync(user_id: str, *connection_ids: str):
    return await sync_oauth_grants(
        user_id=user_id,
        workspace_id=WORKSPACE_ID,
        connection_ids=list(connection_ids),
    )


class TestOwnership:
    @pytest.mark.asyncio
    async def test_the_owner_gets_a_grant(self, db):
        synced = await _sync(OWNER, CONNECTION_ID)
        assert synced.grants == {CONNECTION_ID: f"grant-for-{CONNECTION_ID}"}

    @pytest.mark.asyncio
    async def test_another_users_connection_yields_no_grant(self, db):
        """The id is real, but not theirs — it must bind into no workspace."""
        synced = await _sync(INTRUDER, CONNECTION_ID)
        assert synced.grants == {}

    @pytest.mark.asyncio
    async def test_an_unknown_connection_fails_the_same_way(self, db):
        """Same empty answer as the wrong-owner case: guessing ids teaches nothing."""
        synced = await _sync(OWNER, UNKNOWN_CONNECTION_ID)
        assert synced.grants == {}

    @pytest.mark.asyncio
    async def test_one_bad_id_does_not_cost_the_others_their_grants(self, db):
        synced = await _sync(OWNER, UNKNOWN_CONNECTION_ID, CONNECTION_ID)
        assert synced.grants == {CONNECTION_ID: f"grant-for-{CONNECTION_ID}"}

    @pytest.mark.asyncio
    async def test_the_predicate_is_in_the_sql_not_the_caller(self, db):
        """Pinned structurally too — the fake can only model what the SQL says.

        Were the ownership filter to move out of the statement, every arm above
        would still pass against a differently-shaped fake.
        """
        await _sync(OWNER, CONNECTION_ID)
        sql, params, _depth = db.statements[0]
        flat = re.sub(r"\s+", " ", sql)
        assert "FROM user_mcp_oauth_connections c" in flat
        assert "WHERE c.connection_id = ANY(%s::uuid[]) AND c.user_id = %s" in flat
        # The inserted connection_id AND destination_url both come from the
        # connection row (c.connection_id, c.server_url), never a parameter —
        # a caller can never steer the grant at a host the token wasn't issued
        # for. No destination_url parameter exists to pass.
        assert "SELECT %s, %s::uuid, %s, c.connection_id, c.server_url" in flat
        assert params[-2:] == ([CONNECTION_ID], OWNER)
        assert params[2] == GRANT_KIND_OAUTH_MCP


class TestIdempotence:
    @pytest.mark.asyncio
    async def test_re_syncing_returns_the_same_grant(self, db):
        first = await _sync(OWNER, CONNECTION_ID)
        second = await _sync(OWNER, CONNECTION_ID)
        assert first.grants == second.grants
        assert second.retired == 0


class TestRetirement:
    """The retire predicate is what closes the authorization overhang: an
    active grant the resolved set no longer contains must stop being
    spendable, and the upserted set is the only thing that protects a grant."""

    @pytest.mark.asyncio
    async def test_a_dropped_server_loses_its_grant(self, db):
        await _sync(OWNER, CONNECTION_ID, OTHER_CONNECTION_ID)
        synced = await _sync(OWNER, CONNECTION_ID)

        assert synced.retired == 1
        assert db.active_grant_ids() == {f"grant-for-{CONNECTION_ID}"}

    @pytest.mark.asyncio
    async def test_an_empty_set_retires_everything_active(self, db):
        await _sync(OWNER, CONNECTION_ID, OTHER_CONNECTION_ID)
        synced = await _sync(OWNER)

        assert synced.grants == {}
        assert synced.retired == 2
        assert db.active_grant_ids() == set()
        # Nothing to upsert ⇒ only the retirement statement is issued.
        assert len(db.statements) == 3

    @pytest.mark.asyncio
    async def test_a_connection_that_vanished_loses_its_grant_too(self, db):
        """Its id is still resolved, but it no longer selects a row — the keep
        list is built from what was upserted, never from what was asked for."""
        await _sync(OWNER, CONNECTION_ID)
        db._connections.pop(CONNECTION_ID)
        synced = await _sync(OWNER, CONNECTION_ID)

        assert synced.grants == {}
        assert synced.retired == 1

    @pytest.mark.asyncio
    async def test_upsert_and_retirement_commit_together(self, db):
        await _sync(OWNER, CONNECTION_ID)
        assert [depth for _sql, _params, depth in db.statements] == [1, 1]
