"""Unit tests for the MCP OAuth token lifecycle (refresh single-flight).

The lifecycle's whole job is to hand back a usable access token without ever
letting two workers burn the same one-time refresh token. Four properties
carry that, and each gets its own coverage here:

- the **hot path takes no lock** — with >10 minutes of validity the call is a
  single read, so the common case never touches Postgres' lock manager;
- exactly one **winner** refreshes per cluster (``pg_try_advisory_lock``), and
  it commits under a ``token_generation`` compare-and-swap;
- **losers never block**: a comfortably valid old token is served instantly,
  and only a near-expiry loser briefly polls for the winner's commit;
- an **ambiguous** refresh timeout is terminal for retries — the refresh token
  may already be consumed server-side, so the connection flips to
  ``refresh_ambiguous`` and rides the old access token to expiry.

Redis, Postgres and the network are all faked at the module's seams: the
advisory-lock cursor, the connection-row store, and the token endpoint.
"""

from __future__ import annotations

import copy
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx2
import pytest

from src.server.database import mcp_oauth as mcp_oauth_db
from src.server.services.mcp_oauth import lifecycle
from src.server.services.mcp_oauth.http import OAuthHopBlocked
from src.server.services.mcp_oauth.lifecycle import (
    TokenUnavailable,
    ensure_fresh_access_token,
)
from src.server.services.writer_guard import advisory_key

CONNECTION_ID = "11111111-2222-3333-4444-555555555555"
USER_ID = "user-lifecycle-1"
SERVER_NAME = "demo notes"
SERVER_URL = "https://mcp.demo.test/mcp"
ISSUER = "https://auth.demo.test"
LOCK_KEY = advisory_key("mcp_oauth_refresh", CONNECTION_ID)


def _row(
    *,
    expires_in: float | None = 3600,
    generation: int = 3,
    status: str = "connected",
    access_token: str = "access-old",
    refresh_token: str | None = "refresh-old",
    **overrides,
) -> dict:
    """A decrypted connection row as :func:`get_connection_by_id` hands it over."""
    row = {
        "connection_id": CONNECTION_ID,
        "user_id": USER_ID,
        "server_name": SERVER_NAME,
        "server_url": SERVER_URL,
        "status": status,
        "token_type": "Bearer",
        "scope": "notes.read offline_access",
        "expires_at": (
            None
            if expires_in is None
            else datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        ),
        "token_generation": generation,
        "client_info": {"client_id": "client-abc123"},
        "as_metadata": {"issuer": ISSUER, "token_endpoint": f"{ISSUER}/token"},
        "resource_metadata": None,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "client_secret": None,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeStore:
    """The connection row, plus the two writes the lifecycle may perform.

    ``script`` swaps in a different row on the Nth read, which is how a
    competing worker's commit is made to land mid-flight.
    """

    def __init__(self, row: dict | None):
        self.row = row
        self.script: dict[int, dict | None] = {}
        self.read_count = 0
        self.marks: list[str] = []
        self.commits: list[dict] = []

    async def get_connection_by_id(self, connection_id, *, decrypt=False):
        assert connection_id == CONNECTION_ID
        # The lifecycle needs the plaintext bundle; a summary read is a bug.
        assert decrypt is True
        self.read_count += 1
        if self.read_count in self.script:
            self.row = self.script[self.read_count]
        return copy.deepcopy(self.row) if self.row is not None else None

    async def mark_status(self, connection_id, status):
        self.marks.append(status)
        if self.row is not None:
            self.row["status"] = status
        return True

    async def commit_refresh(
        self,
        connection_id,
        *,
        expected_generation,
        access_token,
        refresh_token,
        expires_at,
        scope=None,
    ):
        self.commits.append(
            {
                "connection_id": connection_id,
                "expected_generation": expected_generation,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": expires_at,
                "scope": scope,
            }
        )
        if self.row is None or self.row["token_generation"] != expected_generation:
            return False  # a newer bundle already landed
        self.row.update(
            access_token=access_token,
            refresh_token=refresh_token or self.row["refresh_token"],
            expires_at=expires_at,
            scope=scope or self.row["scope"],
            token_generation=expected_generation + 1,
            status="connected",
        )
        return True


class _FakeCursor:
    def __init__(self, db: "FakeLockDb"):
        self._db = db

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def execute(self, sql, params=None):
        self._db.statements.append((" ".join(sql.split()), params))

    async def fetchone(self):
        sql, _ = self._db.statements[-1]
        assert "pg_try_advisory_lock" in sql
        return (self._db.acquired,)


class FakeLockDb:
    """Stands in for the pooled connection the try-lock is taken on."""

    def __init__(self, *, acquired: bool = True):
        self.acquired = acquired
        self.statements: list[tuple[str, tuple | None]] = []
        self.opened = 0

    @asynccontextmanager
    async def connection(self):
        self.opened += 1
        yield SimpleNamespace(cursor=lambda *a, **k: _FakeCursor(self))

    def _keys(self, fn: str) -> list[int]:
        return [
            params[0]
            for sql, params in self.statements
            if fn in sql and params is not None
        ]

    @property
    def lock_attempts(self) -> list[int]:
        return self._keys("pg_try_advisory_lock")

    @property
    def unlocks(self) -> list[int]:
        return self._keys("pg_advisory_unlock")


class FakeTokenEndpoint:
    def __init__(self):
        self.calls: list[dict] = []
        self.status_code = 200
        self.payload: dict = {
            "access_token": "access-new",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": "refresh-new",
            "scope": "notes.read offline_access",
        }
        self.raises: Exception | None = None
        # Fires while the refresh is in flight — the window a rival worker's
        # commit would land in.
        self.on_call = None

    async def request(
        self, client, method, url, *, headers=None, data=None, content=None
    ):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "data": data}
        )
        if self.on_call is not None:
            self.on_call()
        if self.raises is not None:
            raise self.raises
        return httpx2.Response(self.status_code, json=self.payload)


@asynccontextmanager
async def _fake_http_client():
    # `timeout` is assigned on the client, so it must be a settable attribute.
    yield SimpleNamespace(name="fake-oauth-client", timeout=None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(monkeypatch) -> FakeStore:
    fake = FakeStore(_row())
    monkeypatch.setattr(lifecycle, "get_connection_by_id", fake.get_connection_by_id)
    monkeypatch.setattr(lifecycle, "mark_status", fake.mark_status)
    monkeypatch.setattr(lifecycle, "commit_refresh", fake.commit_refresh)
    return fake


@pytest.fixture
def db(monkeypatch) -> FakeLockDb:
    fake = FakeLockDb()
    monkeypatch.setattr(
        "src.server.database.pool.get_db_connection", fake.connection
    )
    return fake


@pytest.fixture
def token_endpoint(monkeypatch) -> FakeTokenEndpoint:
    fake = FakeTokenEndpoint()
    monkeypatch.setattr(lifecycle, "pinned_request", fake.request)
    monkeypatch.setattr(lifecycle, "oauth_http_client", _fake_http_client)
    return fake


@pytest.fixture
def short_poll(monkeypatch):
    """One poll iteration instead of eight — the loop shape, not the wall clock."""
    monkeypatch.setattr(lifecycle, "LOSER_POLL_SECONDS", 0.05)


# ---------------------------------------------------------------------------
# Hot path — no lock, no HTTP
# ---------------------------------------------------------------------------


class TestHotPath:
    @pytest.mark.asyncio
    async def test_comfortable_validity_takes_no_lock(self, store, db, token_endpoint):
        store.row = _row(expires_in=3600)

        token = await ensure_fresh_access_token(CONNECTION_ID)

        assert token == {
            "access_token": "access-old",
            "token_type": "Bearer",
            "server_name": SERVER_NAME,
            "status": "connected",
        }
        # The whole point of the margin: one read, and the lock manager is
        # never consulted.
        assert store.read_count == 1
        assert db.opened == 0
        assert db.lock_attempts == []
        assert token_endpoint.calls == []

    @pytest.mark.asyncio
    async def test_non_expiring_token_is_never_refreshed(
        self, store, db, token_endpoint
    ):
        store.row = _row(expires_in=None)

        token = await ensure_fresh_access_token(CONNECTION_ID)

        assert token["access_token"] == "access-old"
        assert db.lock_attempts == []
        assert token_endpoint.calls == []

    @pytest.mark.asyncio
    async def test_a_naive_expiry_is_read_as_utc(self, store, db, token_endpoint):
        # Postgres can hand back a naive timestamp; reading it as local time
        # would misjudge the margin by the UTC offset — enough, in most of the
        # world, to refresh an hour early or serve an expired token.
        naive = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None)
        store.row = _row(expires_at=naive)

        token = await ensure_fresh_access_token(CONNECTION_ID)

        assert token["access_token"] == "access-old"
        assert db.lock_attempts == []

    @pytest.mark.asyncio
    async def test_just_inside_the_margin_does_take_the_lock(
        self, store, db, token_endpoint
    ):
        store.row = _row(expires_in=lifecycle.REFRESH_MARGIN_SECONDS - 30)

        await ensure_fresh_access_token(CONNECTION_ID)

        assert db.lock_attempts == [LOCK_KEY]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "reason"), [("revoked", "revoked"), ("needs_reauth", "needs_reauth")]
    )
    async def test_dead_statuses_short_circuit(
        self, store, db, token_endpoint, status, reason
    ):
        store.row = _row(expires_in=30, status=status)

        with pytest.raises(TokenUnavailable) as excinfo:
            await ensure_fresh_access_token(CONNECTION_ID)

        assert excinfo.value.reason == reason
        assert db.lock_attempts == []
        assert token_endpoint.calls == []

    @pytest.mark.asyncio
    async def test_unknown_connection(self, store, db, token_endpoint):
        store.row = None

        with pytest.raises(TokenUnavailable) as excinfo:
            await ensure_fresh_access_token(CONNECTION_ID)

        assert excinfo.value.reason == "unknown_connection"
        assert db.lock_attempts == []

    @pytest.mark.asyncio
    async def test_no_refresh_token_rides_the_access_token_to_expiry(
        self, store, db, token_endpoint
    ):
        store.row = _row(expires_in=120, refresh_token=None)

        token = await ensure_fresh_access_token(CONNECTION_ID)

        assert token["access_token"] == "access-old"
        assert db.lock_attempts == []
        assert token_endpoint.calls == []
        assert store.marks == []

    @pytest.mark.asyncio
    async def test_no_refresh_token_and_expired_needs_reauth(
        self, store, db, token_endpoint
    ):
        store.row = _row(expires_in=-5, refresh_token=None)

        with pytest.raises(TokenUnavailable) as excinfo:
            await ensure_fresh_access_token(CONNECTION_ID)

        assert excinfo.value.reason == "needs_reauth"
        assert store.marks == ["needs_reauth"]
        assert token_endpoint.calls == []


# ---------------------------------------------------------------------------
# Winner — one refresh, generation-CAS commit, lock always released
# ---------------------------------------------------------------------------


class TestWinner:
    @pytest.mark.asyncio
    async def test_winner_refreshes_once_and_commits_the_next_generation(
        self, store, db, token_endpoint
    ):
        store.row = _row(expires_in=120, generation=3)

        token = await ensure_fresh_access_token(CONNECTION_ID)

        assert token == {
            "access_token": "access-new",
            "token_type": "Bearer",
            "server_name": SERVER_NAME,
            "status": "connected",
        }
        [call] = token_endpoint.calls
        assert call["method"] == "POST"
        assert call["url"] == f"{ISSUER}/token"
        assert call["data"] == {
            "grant_type": "refresh_token",
            "refresh_token": "refresh-old",
            "client_id": "client-abc123",
        }
        [commit] = store.commits
        assert commit["expected_generation"] == 3
        assert commit["access_token"] == "access-new"
        assert commit["refresh_token"] == "refresh-new"
        expected = datetime.now(timezone.utc) + timedelta(seconds=3600)
        assert abs((commit["expires_at"] - expected).total_seconds()) < 30
        # The stored bundle advanced exactly one generation.
        assert store.row["token_generation"] == 4

    @pytest.mark.asyncio
    async def test_an_unrotated_refresh_token_is_kept(
        self, store, db, token_endpoint
    ):
        # An AS that omits refresh_token means "keep the one you have";
        # committing that absence as NULL would blank the only copy.
        store.row = _row(expires_in=120)
        token_endpoint.payload = {
            "access_token": "access-new",
            "token_type": "Bearer",
            "expires_in": 3600,
        }

        token = await ensure_fresh_access_token(CONNECTION_ID)

        assert token["access_token"] == "access-new"
        assert store.commits[0]["refresh_token"] is None
        assert store.row["refresh_token"] == "refresh-old"

    @pytest.mark.asyncio
    async def test_confidential_client_and_resource_are_sent(
        self, store, db, token_endpoint
    ):
        # RFC 8707: a PRM-scoped connection re-asserts its resource on refresh,
        # and a DCR-issued secret is presented in the body.
        store.row = _row(
            expires_in=120,
            client_secret="client-secret-xyz",
            resource_metadata={"resource": SERVER_URL},
        )

        await ensure_fresh_access_token(CONNECTION_ID)

        [call] = token_endpoint.calls
        assert call["data"]["resource"] == SERVER_URL
        assert call["data"]["client_secret"] == "client-secret-xyz"
        assert call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"

    @pytest.mark.asyncio
    async def test_lock_is_taken_and_released_around_the_refresh(
        self, store, db, token_endpoint
    ):
        store.row = _row(expires_in=120)

        await ensure_fresh_access_token(CONNECTION_ID)

        assert db.lock_attempts == [LOCK_KEY]
        assert db.unlocks == [LOCK_KEY]
        assert "pg_try_advisory_lock" in db.statements[0][0]
        assert "pg_advisory_unlock" in db.statements[-1][0]

    @pytest.mark.asyncio
    async def test_lock_is_released_even_when_the_refresh_fails(
        self, store, db, token_endpoint
    ):
        store.row = _row(expires_in=-5)
        token_endpoint.status_code = 400
        token_endpoint.payload = {"error": "invalid_grant"}

        with pytest.raises(TokenUnavailable):
            await ensure_fresh_access_token(CONNECTION_ID)

        assert db.unlocks == [LOCK_KEY]

    @pytest.mark.asyncio
    async def test_re_read_under_the_lock_skips_a_redundant_refresh(
        self, store, db, token_endpoint
    ):
        """The previous winner committed between our read and our lock."""
        store.row = _row(expires_in=120, generation=3)
        store.script[2] = _row(
            expires_in=3600, generation=4, access_token="access-newer"
        )

        token = await ensure_fresh_access_token(CONNECTION_ID)

        assert token["access_token"] == "access-newer"
        assert token_endpoint.calls == []
        assert store.commits == []
        assert db.unlocks == [LOCK_KEY]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [400, 401])
    async def test_definitive_rejection_needs_reauth(
        self, store, db, token_endpoint, status_code
    ):
        store.row = _row(expires_in=120)
        token_endpoint.status_code = status_code
        token_endpoint.payload = {"error": "invalid_grant"}

        with pytest.raises(TokenUnavailable) as excinfo:
            await ensure_fresh_access_token(CONNECTION_ID)

        assert excinfo.value.reason == "needs_reauth"
        assert store.marks == ["needs_reauth"]
        assert store.commits == []

    @pytest.mark.asyncio
    async def test_server_error_keeps_serving_the_old_token(
        self, store, db, token_endpoint
    ):
        store.row = _row(expires_in=120)
        token_endpoint.status_code = 503
        token_endpoint.payload = {"error": "temporarily_unavailable"}

        token = await ensure_fresh_access_token(CONNECTION_ID)

        assert token["access_token"] == "access-old"
        # A 5xx is transient: the connection stays connected and retryable.
        assert store.marks == []
        assert store.commits == []

    @pytest.mark.asyncio
    async def test_lost_cas_falls_back_to_whatever_is_current(
        self, store, db, token_endpoint
    ):
        store.row = _row(expires_in=120, generation=3)

        def _rival_commits_first():
            store.row = _row(
                expires_in=3600, generation=9, access_token="access-rival"
            )

        # A competing bundle lands while our refresh is in flight, so our own
        # commit is a generation behind by the time it runs.
        token_endpoint.on_call = _rival_commits_first

        token = await ensure_fresh_access_token(CONNECTION_ID)

        assert [c["expected_generation"] for c in store.commits] == [3]
        assert token["access_token"] == "access-rival"

    @pytest.mark.asyncio
    async def test_missing_token_endpoint_needs_reauth(
        self, store, db, token_endpoint
    ):
        store.row = _row(expires_in=120, as_metadata={"issuer": ISSUER})

        with pytest.raises(TokenUnavailable) as excinfo:
            await ensure_fresh_access_token(CONNECTION_ID)

        assert excinfo.value.reason == "needs_reauth"
        assert store.marks == ["needs_reauth"]
        assert token_endpoint.calls == []


# ---------------------------------------------------------------------------
# Losers — never block on the winner
# ---------------------------------------------------------------------------


class TestLoser:
    @pytest.fixture
    def db(self, monkeypatch) -> FakeLockDb:
        fake = FakeLockDb(acquired=False)
        monkeypatch.setattr(
            "src.server.database.pool.get_db_connection", fake.connection
        )
        return fake

    @pytest.mark.asyncio
    async def test_still_valid_old_token_is_served_immediately(
        self, store, db, token_endpoint
    ):
        store.row = _row(expires_in=300)  # > the 60s floor

        token = await ensure_fresh_access_token(CONNECTION_ID)

        assert token["access_token"] == "access-old"
        assert db.lock_attempts == [LOCK_KEY]
        assert db.unlocks == []  # a loser holds nothing to release
        assert token_endpoint.calls == []
        # No polling: the read count is the single up-front read.
        assert store.read_count == 1

    @pytest.mark.asyncio
    async def test_near_expiry_loser_polls_and_picks_up_the_winners_commit(
        self, store, db, token_endpoint, short_poll
    ):
        store.row = _row(expires_in=10, generation=3)  # under the 60s floor
        store.script[2] = _row(
            expires_in=3600, generation=4, access_token="access-new"
        )

        token = await ensure_fresh_access_token(CONNECTION_ID)

        assert token["access_token"] == "access-new"
        assert store.read_count == 2
        assert token_endpoint.calls == []

    @pytest.mark.asyncio
    async def test_near_expiry_loser_falls_back_to_the_old_token(
        self, store, db, token_endpoint, short_poll
    ):
        """No commit arrives, but the old token still has seconds left."""
        store.row = _row(expires_in=10, generation=3)

        token = await ensure_fresh_access_token(CONNECTION_ID)

        assert token["access_token"] == "access-old"
        assert token_endpoint.calls == []

    @pytest.mark.asyncio
    async def test_expired_loser_reports_refresh_in_progress(
        self, store, db, token_endpoint, short_poll
    ):
        store.row = _row(expires_in=-5, generation=3)

        with pytest.raises(TokenUnavailable) as excinfo:
            await ensure_fresh_access_token(CONNECTION_ID)

        assert excinfo.value.reason == "refresh_in_progress"
        assert token_endpoint.calls == []
        assert store.commits == []

    @pytest.mark.asyncio
    async def test_a_stale_generation_bump_is_not_mistaken_for_a_refresh(
        self, store, db, token_endpoint, short_poll
    ):
        """A newer generation that is itself already expired is not usable."""
        store.row = _row(expires_in=-5, generation=3)
        store.script[2] = _row(expires_in=-1, generation=4, access_token="access-dud")

        with pytest.raises(TokenUnavailable) as excinfo:
            await ensure_fresh_access_token(CONNECTION_ID)

        assert excinfo.value.reason == "refresh_in_progress"


# ---------------------------------------------------------------------------
# Ambiguous refresh — never retried
# ---------------------------------------------------------------------------


class TestAmbiguousRefresh:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure",
        [
            httpx2.ReadTimeout("token endpoint timed out"),
            httpx2.ConnectTimeout("token endpoint connect timed out"),
            OAuthHopBlocked("token endpoint hop blocked mid-flight"),
        ],
    )
    async def test_timeout_flips_to_ambiguous_and_keeps_the_old_token(
        self, store, db, token_endpoint, failure
    ):
        store.row = _row(expires_in=120)
        token_endpoint.raises = failure

        token = await ensure_fresh_access_token(CONNECTION_ID)

        assert token["access_token"] == "access-old"
        assert store.marks == ["refresh_ambiguous"]
        assert store.commits == []
        assert len(token_endpoint.calls) == 1

    @pytest.mark.asyncio
    async def test_ambiguous_refresh_is_never_retried(
        self, store, db, token_endpoint
    ):
        store.row = _row(expires_in=120)
        token_endpoint.raises = httpx2.ReadTimeout("token endpoint timed out")

        first = await ensure_fresh_access_token(CONNECTION_ID)
        token_endpoint.raises = None  # the endpoint recovers; we still must not ask
        second = await ensure_fresh_access_token(CONNECTION_ID)
        third = await ensure_fresh_access_token(CONNECTION_ID)

        assert first["access_token"] == "access-old"
        assert second["access_token"] == "access-old"
        assert third["access_token"] == "access-old"
        # The refresh token may already be consumed server-side: one attempt,
        # ever. Later calls do not even reach for the lock.
        assert len(token_endpoint.calls) == 1
        assert db.lock_attempts == [LOCK_KEY]
        assert store.marks == ["refresh_ambiguous"]

    @pytest.mark.asyncio
    async def test_ambiguous_connection_needs_reauth_once_the_token_expires(
        self, store, db, token_endpoint
    ):
        store.row = _row(expires_in=-1, status="refresh_ambiguous")

        with pytest.raises(TokenUnavailable) as excinfo:
            await ensure_fresh_access_token(CONNECTION_ID)

        assert excinfo.value.reason == "needs_reauth"
        assert store.marks == ["needs_reauth"]
        assert token_endpoint.calls == []
        assert db.lock_attempts == []

    @pytest.mark.asyncio
    async def test_transport_error_is_not_ambiguous(
        self, store, db, token_endpoint
    ):
        """A refused connection cannot have consumed the refresh token."""
        store.row = _row(expires_in=120)
        token_endpoint.raises = httpx2.ConnectError("connection refused")

        token = await ensure_fresh_access_token(CONNECTION_ID)

        assert token["access_token"] == "access-old"
        assert store.marks == []  # status untouched — the next call may retry

        token_endpoint.raises = None
        again = await ensure_fresh_access_token(CONNECTION_ID)

        assert again["access_token"] == "access-new"
        assert len(token_endpoint.calls) == 2

    @pytest.mark.asyncio
    async def test_transport_error_on_an_expired_token_reports_expired(
        self, store, db, token_endpoint
    ):
        store.row = _row(expires_in=-5)
        token_endpoint.raises = httpx2.ConnectError("connection refused")

        with pytest.raises(TokenUnavailable) as excinfo:
            await ensure_fresh_access_token(CONNECTION_ID)

        assert excinfo.value.reason == "expired"
        assert store.marks == []


# ---------------------------------------------------------------------------
# The commit itself — generation compare-and-swap
# ---------------------------------------------------------------------------


class _CasCursor:
    """Mimics the UPDATE's WHERE clause: rowcount 1 only on a generation hit."""

    def __init__(self, state: dict, log: list[str]):
        self._state = state
        self._log = log
        self.rowcount = 0

    async def __aenter__(self) -> "_CasCursor":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def execute(self, sql, params=None):
        self._log.append(" ".join(sql.split()))
        access_token = params[0]
        connection_id, expected_generation = params[-2], params[-1]
        state = self._state
        if (
            connection_id == state["connection_id"]
            and expected_generation == state["token_generation"]
            and state["status"] in ("connected", "refresh_ambiguous")
        ):
            state["token_generation"] += 1
            state["access_token"] = access_token
            state["status"] = "connected"
            self.rowcount = 1
        else:
            self.rowcount = 0


class TestGenerationCas:
    @pytest.fixture
    def cas(self, monkeypatch):
        monkeypatch.setenv("BYOK_ENCRYPTION_KEY", "unit-test-key")
        state = {
            "connection_id": CONNECTION_ID,
            "token_generation": 7,
            "access_token": "access-old",
            "status": "connected",
        }
        log: list[str] = []

        @asynccontextmanager
        async def _conn():
            yield SimpleNamespace(cursor=lambda *a, **k: _CasCursor(state, log))

        monkeypatch.setattr(mcp_oauth_db, "get_db_connection", _conn)
        return SimpleNamespace(state=state, sql=log)

    async def _commit(self, generation: int, access_token: str) -> bool:
        return await mcp_oauth_db.commit_refresh(
            CONNECTION_ID,
            expected_generation=generation,
            access_token=access_token,
            refresh_token="refresh-new",
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=3600),
            scope="notes.read offline_access",
        )

    @pytest.mark.asyncio
    async def test_second_winner_with_a_stale_generation_does_not_land(self, cas):
        first = await self._commit(7, "access-winner")
        second = await self._commit(7, "access-loser")

        assert first is True
        assert second is False
        # The loser's rotation must not overwrite the surviving bundle.
        assert cas.state["access_token"] == "access-winner"
        assert cas.state["token_generation"] == 8

    @pytest.mark.asyncio
    async def test_the_loser_can_commit_once_it_re_reads(self, cas):
        await self._commit(7, "access-winner")

        assert await self._commit(8, "access-second") is True
        assert cas.state["token_generation"] == 9

    @pytest.mark.asyncio
    async def test_a_revoked_connection_rejects_the_commit(self, cas):
        cas.state["status"] = "revoked"

        assert await self._commit(7, "access-winner") is False
        assert cas.state["access_token"] == "access-old"

    @pytest.mark.asyncio
    async def test_an_ambiguous_connection_can_still_be_repaired(self, cas):
        cas.state["status"] = "refresh_ambiguous"

        assert await self._commit(7, "access-repaired") is True
        assert cas.state["status"] == "connected"

    @pytest.mark.asyncio
    async def test_the_update_is_a_compare_and_swap_on_the_generation(self, cas):
        await self._commit(7, "access-winner")

        [sql] = cas.sql
        assert "token_generation = token_generation + 1" in sql
        assert "AND token_generation = %s" in sql
        assert "AND status IN ('connected', 'refresh_ambiguous')" in sql

    @pytest.mark.asyncio
    async def test_a_null_refresh_token_keeps_the_stored_one(self, cas):
        # The UPDATE must branch on NULL rather than encrypt it, or an AS that
        # skips rotation would cost us the refresh token.
        landed = await mcp_oauth_db.commit_refresh(
            CONNECTION_ID,
            expected_generation=7,
            access_token="access-new",
            refresh_token=None,
            expires_at=None,
        )

        assert landed is True
        [sql] = cas.sql
        assert "refresh_token = CASE WHEN %s::text IS NULL THEN refresh_token" in sql


# ---------------------------------------------------------------------------
# The DB-layer → lifecycle row contract
# ---------------------------------------------------------------------------


class _RowCursor:
    """Answers the single-row SELECT with a psycopg-shaped row."""

    def __init__(self, row: dict):
        self._row = row

    async def __aenter__(self) -> "_RowCursor":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def execute(self, sql, params=None):
        self._row.setdefault("_executed", True)

    async def fetchone(self):
        return self._row


class TestRowShapeContract:
    """``get_connection_by_id`` feeds ``_expiry_seconds`` directly, so what it
    returns has to be native types — not the UI's serialized view.

    Regression lock: the decrypted read once went through ``_row_summary``,
    which ISO-serializes ``expires_at``; every refresh-due call then died on
    ``'str' object has no attribute 'tzinfo'``. These drive the real DB helper
    against a faked cursor, so they fail if that routing ever comes back.
    """

    EXPIRES_IN = 900

    @pytest.fixture
    def db_row(self, monkeypatch) -> dict:
        monkeypatch.setenv("BYOK_ENCRYPTION_KEY", "unit-test-key")
        now = datetime.now(timezone.utc)
        row = {
            # psycopg hands back a UUID object, not a string.
            "connection_id": uuid.UUID(CONNECTION_ID),
            "user_id": USER_ID,
            "server_name": SERVER_NAME,
            "server_url": SERVER_URL,
            "status": "connected",
            "token_type": "Bearer",
            "scope": "notes.read offline_access",
            "expires_at": now + timedelta(seconds=self.EXPIRES_IN),
            "token_generation": 3,
            "client_info": {"client_id": "client-abc123"},
            "as_metadata": {"issuer": ISSUER},
            "resource_metadata": None,
            "last_refresh_at": None,
            "created_at": now,
            "updated_at": now,
            "access_token_plain": "access-old",
            "refresh_token_plain": "refresh-old",
            "client_secret_plain": None,
        }

        @asynccontextmanager
        async def _conn():
            yield SimpleNamespace(cursor=lambda *a, **k: _RowCursor(row))

        monkeypatch.setattr(mcp_oauth_db, "get_db_connection", _conn)
        return row

    @pytest.mark.asyncio
    async def test_expires_at_survives_as_a_datetime(self, db_row):
        out = await mcp_oauth_db.get_connection_by_id(CONNECTION_ID, decrypt=True)

        assert isinstance(out["expires_at"], datetime)
        assert out["expires_at"] == db_row["expires_at"]
        # The consumer that broke: expiry math straight off the returned row.
        remaining = lifecycle._expiry_seconds(out)
        assert isinstance(remaining, float)
        assert abs(remaining - self.EXPIRES_IN) < 30

    @pytest.mark.asyncio
    async def test_a_db_layer_row_drives_the_hot_path_end_to_end(
        self, db_row, store, db, token_endpoint
    ):
        # The two layers joined: the row the DB helper really produces, handed
        # to the lifecycle unmodified. This is the exact call that used to
        # raise AttributeError before the row shape was fixed.
        store.row = await mcp_oauth_db.get_connection_by_id(
            CONNECTION_ID, decrypt=True
        )

        token = await ensure_fresh_access_token(CONNECTION_ID)

        assert token["access_token"] == "access-old"
        assert token["server_name"] == SERVER_NAME
        # 900s left is outside the 600s margin, so this is the no-lock path.
        assert db.lock_attempts == []
        assert token_endpoint.calls == []

    @pytest.mark.asyncio
    async def test_decrypted_plaintext_is_mapped_and_raw_columns_dropped(self, db_row):
        out = await mcp_oauth_db.get_connection_by_id(CONNECTION_ID, decrypt=True)

        assert out["access_token"] == "access-old"
        assert out["refresh_token"] == "refresh-old"
        assert out["client_secret"] is None
        # The ciphertext-column aliases must not ride along.
        assert not [k for k in out if k.endswith("_plain")]

    @pytest.mark.asyncio
    async def test_a_summary_read_never_carries_token_plaintext(self, db_row):
        out = await mcp_oauth_db.get_connection_by_id(CONNECTION_ID)

        assert "access_token" not in out
        assert "refresh_token" not in out
        assert not [k for k in out if k.endswith("_plain")]

    @pytest.mark.asyncio
    async def test_connection_id_is_stringified(self, db_row):
        out = await mcp_oauth_db.get_connection_by_id(CONNECTION_ID, decrypt=True)

        # Callers interpolate it into advisory-lock keys and log lines.
        assert out["connection_id"] == CONNECTION_ID
        assert isinstance(out["connection_id"], str)

    def test_the_ui_view_still_serializes_timestamps(self, db_row):
        # The other half of the split the fix established: list_connections is
        # a JSON view, so _row_summary keeps ISO strings. Don't unify these.
        summary = mcp_oauth_db._row_summary(db_row)

        assert isinstance(summary["expires_at"], str)
        assert summary["expires_at"] == db_row["expires_at"].isoformat()
