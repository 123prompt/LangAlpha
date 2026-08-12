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

``disconnect_server`` is the module's other write path, and it gets the same
treatment at the end of the file: its three revocation writes commit as one.

Redis, Postgres and the network are all faked at the module's seams: the
advisory-lock cursor, the connection-row store, and the token endpoint.
"""

from __future__ import annotations

import dataclasses
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx2
import pytest

from src.server.database import mcp_oauth as mcp_oauth_db
from src.server.database.mcp_oauth import (
    BearerBundle,
    ConnectionStatus,
    ConnectionSummary,
    RefreshBundle,
    Secrets,
)
from src.server.services.mcp_oauth import lifecycle, tokens
from src.server.services.mcp_oauth.http import OAuthHopBlocked
from src.server.services.mcp_oauth.lifecycle import (
    AccessToken,
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
) -> RefreshBundle:
    """A connection as :func:`get_connection_by_id` hands it over, fully read."""
    now = datetime.now(timezone.utc)
    fields = {
        "connection_id": CONNECTION_ID,
        "user_id": USER_ID,
        "server_name": SERVER_NAME,
        "server_url": SERVER_URL,
        "status": ConnectionStatus(status),
        "token_type": "Bearer",
        "scope": "notes.read offline_access",
        "expires_at": (
            None if expires_in is None else now + timedelta(seconds=expires_in)
        ),
        "token_generation": generation,
        "client_info": {"client_id": "client-abc123"},
        "as_metadata": {"issuer": ISSUER, "token_endpoint": f"{ISSUER}/token"},
        "resource_metadata": None,
        "has_refresh_token": refresh_token is not None,
        "last_refresh_at": None,
        "created_at": now,
        "updated_at": now,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "client_secret": None,
    }
    fields.update(overrides)
    return RefreshBundle(**fields)


def _project(row: RefreshBundle, secrets: Secrets) -> ConnectionSummary:
    """Mirror the real read: a mode carries only the columns it decrypted.

    Code that reaches for the refresh token or client secret while asking for
    BEARER fails here exactly as it would against Postgres.
    """
    fields = {f.name: getattr(row, f.name) for f in dataclasses.fields(row)}
    if secrets is Secrets.FULL:
        return RefreshBundle(**fields)
    for name in ("refresh_token", "client_secret"):
        fields.pop(name)
    if secrets is Secrets.BEARER:
        return BearerBundle(**fields)
    fields.pop("access_token")
    return ConnectionSummary(**fields)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeStore:
    """The connection row, plus the two writes the lifecycle may perform.

    ``script`` swaps in a different row on the Nth read, which is how a
    competing worker's commit is made to land mid-flight.
    """

    def __init__(self, row: RefreshBundle | None):
        self.row = row
        self.script: dict[int, RefreshBundle | None] = {}
        self.read_count = 0
        self.marks: list[str] = []
        self.commits: list[dict] = []
        self.reads: list[Secrets] = []
        # The connection each write/read was handed — None means it acquired
        # its own from the pool. The refresh winner must thread the held,
        # advisory-locked connection through so it never nests a second acquire.
        self.read_conns: list = []
        self.commit_conns: list = []

    async def get_connection_by_id(self, connection_id, *, secrets=Secrets.NONE, conn=None):
        assert connection_id == CONNECTION_ID
        # The lifecycle always needs at least the bearer; a summary read is a bug.
        assert secrets is not Secrets.NONE
        self.reads.append(secrets)
        self.read_conns.append(conn)
        self.read_count += 1
        if self.read_count in self.script:
            self.row = self.script[self.read_count]
        if self.row is None:
            return None
        return _project(self.row, secrets)

    async def mark_status(self, connection_id, status, *, conn=None):
        self.marks.append(status)
        if self.row is not None:
            self.row = dataclasses.replace(self.row, status=ConnectionStatus(status))
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
        conn=None,
    ):
        self.commit_conns.append(conn)
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
        if self.row is None or self.row.token_generation != expected_generation:
            return False  # a newer bundle already landed
        surviving = refresh_token or self.row.refresh_token
        self.row = dataclasses.replace(
            self.row,
            access_token=access_token,
            refresh_token=surviving,
            has_refresh_token=surviving is not None,
            expires_at=expires_at,
            scope=scope or self.row.scope,
            token_generation=expected_generation + 1,
            status=ConnectionStatus.CONNECTED,
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
        self.last_conn = None

    @asynccontextmanager
    async def connection(self):
        self.opened += 1
        self.last_conn = SimpleNamespace(cursor=lambda *a, **k: _FakeCursor(self))
        yield self.last_conn

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
    # The token POST lives in mcp_oauth.tokens — the one place both the refresh
    # and the connect-time code exchange go through.
    monkeypatch.setattr(tokens, "pinned_request", fake.request)
    monkeypatch.setattr(tokens, "oauth_http_client", _fake_http_client)
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

        assert token == AccessToken(
            access_token="access-old", token_type="Bearer", generation=3
        )
        # The whole point of the margin: one read, and the lock manager is
        # never consulted.
        assert store.read_count == 1
        assert db.opened == 0
        assert db.lock_attempts == []
        assert token_endpoint.calls == []

    @pytest.mark.asyncio
    async def test_the_relayed_call_path_decrypts_the_bearer_only(
        self, store, db, token_endpoint
    ):
        # Every relayed tool call lands here, and each decrypted column re-runs
        # OpenPGP S2K on the DB. The refresh token and client secret are not
        # needed to serve a valid bearer, and the refresh path re-reads the full
        # bundle under the lock anyway — so widening this read back to FULL is a
        # pure regression, and this is the only place that would notice.
        store.row = _row(expires_in=3600)

        await ensure_fresh_access_token(CONNECTION_ID)

        assert store.reads == [Secrets.BEARER]

    @pytest.mark.asyncio
    async def test_no_refresh_token_is_decided_without_decrypting_one(
        self, store, db, token_endpoint
    ):
        # The "can this connection refresh?" question is answered by the
        # column's NOT NULL-ness, so it survives a bearer-only read.
        store.row = _row(expires_in=120, refresh_token=None)

        token = await ensure_fresh_access_token(CONNECTION_ID)

        # Inside the refresh margin, but with nothing to refresh with: ride the
        # old token to expiry rather than attempting a doomed refresh.
        assert token.access_token == "access-old"
        assert store.reads == [Secrets.BEARER]
        assert token_endpoint.calls == []

    @pytest.mark.asyncio
    async def test_a_missing_token_type_is_defaulted_here_not_at_the_caller(
        self, store, db, token_endpoint
    ):
        """Vendors may omit token_type; every holder must still get a header.

        The default belongs on this side of the boundary — an AccessToken that
        can be constructed without a scheme is one every consumer has to
        re-defend against.
        """
        store.row = _row(token_type=None)

        token = await ensure_fresh_access_token(CONNECTION_ID)

        assert token.token_type == "Bearer"
        assert token.header() == "Bearer access-old"

    @pytest.mark.asyncio
    async def test_non_expiring_token_is_never_refreshed(
        self, store, db, token_endpoint
    ):
        store.row = _row(expires_in=None)

        token = await ensure_fresh_access_token(CONNECTION_ID)

        assert token.access_token == "access-old"
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

        assert token.access_token == "access-old"
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

        assert token.access_token == "access-old"
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

        # generation 4: the CAS committed exactly one bump over the row we read.
        assert token == AccessToken(
            access_token="access-new", token_type="Bearer", generation=4
        )
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
        assert store.row.token_generation == 4

    @pytest.mark.asyncio
    async def test_the_under_lock_re_read_is_the_one_that_takes_the_full_bundle(
        self, store, db, token_endpoint
    ):
        # The counterpart to the bearer-only hot path: the refresh actually
        # spends the refresh token and client secret, so its re-read — and only
        # its re-read — pays for the full decrypt.
        store.row = _row(expires_in=120)

        await ensure_fresh_access_token(CONNECTION_ID)

        assert store.reads == [Secrets.BEARER, Secrets.FULL]

    @pytest.mark.asyncio
    async def test_under_lock_work_reuses_the_held_connection(
        self, store, db, token_endpoint
    ):
        # The refresh winner holds one advisory-locked pool connection and must
        # run its FULL re-read and commit on THAT connection — never nest a
        # second pool acquire inside the first (which stalls every winner on
        # pool timeout under a many-connection refresh storm). The hot-path
        # bearer read, by contrast, acquires its own (conn is None).
        store.row = _row(expires_in=120)

        await ensure_fresh_access_token(CONNECTION_ID)

        assert store.read_conns[0] is None  # hot-path bearer read
        assert store.read_conns[1] is db.last_conn  # under-lock FULL re-read
        assert store.commit_conns == [db.last_conn]

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

        assert token.access_token == "access-new"
        assert store.commits[0]["refresh_token"] is None
        assert store.row.refresh_token == "refresh-old"

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

        assert token.access_token == "access-newer"
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

        assert token.access_token == "access-old"
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
        assert token.access_token == "access-rival"

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

        assert token.access_token == "access-old"
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

        assert token.access_token == "access-new"
        assert store.read_count == 2
        assert token_endpoint.calls == []

    @pytest.mark.asyncio
    async def test_near_expiry_loser_falls_back_to_the_old_token(
        self, store, db, token_endpoint, short_poll
    ):
        """No commit arrives, but the old token still has seconds left."""
        store.row = _row(expires_in=10, generation=3)

        token = await ensure_fresh_access_token(CONNECTION_ID)

        assert token.access_token == "access-old"
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

        assert token.access_token == "access-old"
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

        assert first.access_token == "access-old"
        assert second.access_token == "access-old"
        assert third.access_token == "access-old"
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

        assert token.access_token == "access-old"
        assert store.marks == []  # status untouched — the next call may retry

        token_endpoint.raises = None
        again = await ensure_fresh_access_token(CONNECTION_ID)

        assert again.access_token == "access-new"
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

    def __init__(self, state: dict, log: list[str], params_log: list[tuple]):
        self._state = state
        self._log = log
        self._params_log = params_log
        self.rowcount = 0

    async def __aenter__(self) -> "_CasCursor":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def execute(self, sql, params=None):
        self._log.append(" ".join(sql.split()))
        self._params_log.append(params)
        access_token = params[0]
        # Trailing params, in SQL order: the status to set, then the three the
        # WHERE clause reads.
        new_status, connection_id, expected_generation, servable = params[-4:]
        state = self._state
        if (
            connection_id == state["connection_id"]
            and expected_generation == state["token_generation"]
            and state["status"] in servable
        ):
            state["token_generation"] += 1
            state["access_token"] = access_token
            state["status"] = new_status
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
        params_log: list[tuple] = []

        @asynccontextmanager
        async def _conn(conn=None):
            yield SimpleNamespace(
                cursor=lambda *a, **k: _CasCursor(state, log, params_log)
            )

        monkeypatch.setattr(mcp_oauth_db, "get_db_connection", _conn)
        return SimpleNamespace(state=state, sql=log, params=params_log)

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
        # The servable set rides in as a parameter, not as inlined literals.
        assert "AND status = ANY(%s)" in sql
        assert cas.params[-1][-1] == ["connected", "refresh_ambiguous"]

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
# Reporting a vendor 401 — the other compare-and-swap
# ---------------------------------------------------------------------------


class _ReauthCursor:
    """Mimics the needs_reauth UPDATE's WHERE clause."""

    def __init__(self, state: dict, log: list[str]):
        self._state = state
        self._log = log
        self.rowcount = 0

    async def __aenter__(self) -> "_ReauthCursor":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def execute(self, sql, params=None):
        self._log.append(" ".join(sql.split()))
        new_status, connection_id, expected_generation, required_status = params
        state = self._state
        if (
            connection_id == state["connection_id"]
            and expected_generation == state["token_generation"]
            and state["status"] == required_status
        ):
            state["status"] = new_status
            self.rowcount = 1
        else:
            self.rowcount = 0


class TestNeedsReauthCas:
    """The relay reports which bundle a vendor rejected; this decides if it lands.

    Moving the decision here is the point: the relay observed a 401 at some
    instant, and by the time the write runs that observation may already be
    stale — only the row itself can adjudicate that.
    """

    @pytest.fixture
    def cas(self, monkeypatch):
        state = {
            "connection_id": CONNECTION_ID,
            "token_generation": 7,
            "status": "connected",
        }
        log: list[str] = []

        @asynccontextmanager
        async def _conn(conn=None):
            yield SimpleNamespace(cursor=lambda *a, **k: _ReauthCursor(state, log))

        monkeypatch.setattr(mcp_oauth_db, "get_db_connection", _conn)
        return SimpleNamespace(state=state, sql=log)

    @pytest.mark.asyncio
    async def test_the_rejected_generation_flips_the_connection(self, cas):
        assert await lifecycle.mark_connection_needs_reauth(
            CONNECTION_ID, seen_token_generation=7
        ) is True
        assert cas.state["status"] == "needs_reauth"

    @pytest.mark.asyncio
    async def test_a_rotation_since_the_401_makes_the_report_moot(self, cas):
        """Another worker refreshed after the vendor said no: the stored bundle
        is not the one that was rejected, so it must survive."""
        cas.state["token_generation"] = 8

        assert await lifecycle.mark_connection_needs_reauth(
            CONNECTION_ID, seen_token_generation=7
        ) is False
        assert cas.state["status"] == "connected"

    @pytest.mark.asyncio
    async def test_a_terminal_status_is_not_overwritten(self, cas):
        # refresh_ambiguous carries strictly more information (never retry the
        # refresh token) than needs_reauth; downgrading it would lose that.
        cas.state["status"] = "refresh_ambiguous"

        assert await lifecycle.mark_connection_needs_reauth(
            CONNECTION_ID, seen_token_generation=7
        ) is False
        assert cas.state["status"] == "refresh_ambiguous"

    @pytest.mark.asyncio
    async def test_a_second_report_of_the_same_generation_is_a_no_op(self, cas):
        first = await lifecycle.mark_connection_needs_reauth(
            CONNECTION_ID, seen_token_generation=7
        )
        second = await lifecycle.mark_connection_needs_reauth(
            CONNECTION_ID, seen_token_generation=7
        )

        assert (first, second) == (True, False)
        assert cas.state["status"] == "needs_reauth"

    @pytest.mark.asyncio
    async def test_both_guards_ride_in_one_statement(self, cas):
        """A read-then-write would reopen the window this exists to close."""
        await lifecycle.mark_connection_needs_reauth(
            CONNECTION_ID, seen_token_generation=7
        )

        [sql] = cas.sql
        assert "AND token_generation = %s" in sql
        assert "AND status = %s" in sql


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
            "has_refresh_token": True,
            "last_refresh_at": None,
            "created_at": now,
            "updated_at": now,
            "access_token_plain": "access-old",
            "refresh_token_plain": "refresh-old",
            "client_secret_plain": None,
        }

        @asynccontextmanager
        async def _conn(conn=None):
            yield SimpleNamespace(cursor=lambda *a, **k: _RowCursor(row))

        monkeypatch.setattr(mcp_oauth_db, "get_db_connection", _conn)
        return row

    @pytest.mark.asyncio
    async def test_expires_at_survives_as_a_datetime(self, db_row):
        out = await mcp_oauth_db.get_connection_by_id(CONNECTION_ID, secrets=Secrets.FULL)

        assert isinstance(out.expires_at, datetime)
        assert out.expires_at == db_row["expires_at"]
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
        store.row = await mcp_oauth_db.get_connection_by_id(CONNECTION_ID, secrets=Secrets.FULL)

        token = await ensure_fresh_access_token(CONNECTION_ID)

        assert token.access_token == "access-old"
        # The generation rides across the layer boundary as an int — it is what
        # a later rotation check compares against.
        assert token.generation == db_row["token_generation"]
        # 900s left is outside the 600s margin, so this is the no-lock path.
        assert db.lock_attempts == []
        assert token_endpoint.calls == []

    @pytest.mark.asyncio
    async def test_decrypted_plaintext_is_mapped_onto_the_full_record(self, db_row):
        out = await mcp_oauth_db.get_connection_by_id(CONNECTION_ID, secrets=Secrets.FULL)

        assert isinstance(out, RefreshBundle)
        assert out.access_token == "access-old"
        assert out.refresh_token == "refresh-old"
        assert out.client_secret is None
        # The ciphertext-column aliases must not ride along.
        assert not [name for name in dir(out) if name.endswith("_plain")]

    @pytest.mark.asyncio
    async def test_a_summary_read_cannot_reach_token_plaintext(self, db_row):
        # The record IS the mode: a read that paid for no decrypt has nowhere
        # to put a token, so "did this reader ask for the bearer?" stops being
        # a convention and becomes a type.
        out = await mcp_oauth_db.get_connection_by_id(CONNECTION_ID)

        assert type(out) is ConnectionSummary
        assert not hasattr(out, "access_token")
        assert not hasattr(out, "refresh_token")
        # ...while still answering whether a refresh is possible at all.
        assert out.has_refresh_token is True

    @pytest.mark.asyncio
    async def test_connection_id_is_stringified(self, db_row):
        out = await mcp_oauth_db.get_connection_by_id(CONNECTION_ID, secrets=Secrets.FULL)

        # Callers interpolate it into advisory-lock keys and log lines.
        assert out.connection_id == CONNECTION_ID
        assert isinstance(out.connection_id, str)

    def test_the_ui_view_still_serializes_timestamps(self, db_row):
        # The other half of the split the fix established: list_connections is
        # a JSON view, so _row_summary keeps ISO strings. Don't unify these.
        summary = mcp_oauth_db._row_summary(db_row)

        assert isinstance(summary["expires_at"], str)
        assert summary["expires_at"] == db_row["expires_at"].isoformat()


# ---------------------------------------------------------------------------
# disconnect_server — the revocation writes commit together
# ---------------------------------------------------------------------------


class FakeDisconnectDb:
    """Records, per write, which connection it ran on and the open-transaction
    depth at the time — the two things atomicity here consists of."""

    def __init__(self) -> None:
        self.conn = SimpleNamespace(transaction=self._transaction)
        self.depth = 0
        self.writes: list[tuple[str, object, int]] = []

    @asynccontextmanager
    async def _transaction(self):
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1

    @asynccontextmanager
    async def connection(self):
        yield self.conn

    def write(self, name: str):
        async def _recorded(*args, conn=None, **kwargs):
            self.writes.append((name, conn, self.depth))
            return 1

        return _recorded

    @property
    def trace(self) -> list[tuple[str, bool, int]]:
        return [(name, c is self.conn, depth) for name, c, depth in self.writes]


@pytest.fixture
def disconnect_db(monkeypatch) -> FakeDisconnectDb:
    fake = FakeDisconnectDb()
    monkeypatch.setattr(
        "src.server.database.pool.get_db_connection", fake.connection
    )
    monkeypatch.setattr(lifecycle, "mark_status", fake.write("mark_status"))
    monkeypatch.setattr(
        lifecycle, "revoke_grants_for_connection", fake.write("revoke_grants")
    )
    monkeypatch.setattr(
        "src.server.database.mcp_tool_schemas.delete_user_tool_schemas",
        fake.write("delete_schemas"),
    )
    monkeypatch.setattr(
        "src.server.database.mcp_servers.bump_user_workspaces_mcp_version",
        fake.write("bump_versions"),
    )
    return fake


def _connected(row=None):
    async def _get_connection(user_id, server_name, **kwargs):
        return row

    return _get_connection


@pytest.mark.asyncio
class TestDisconnectAtomicity:
    """A half-applied disconnect disagrees with itself — grants revoked while
    the row still reads connected leaves the sweeper renewing a credential the
    user gave up. All three writes therefore share one transaction."""

    async def test_the_three_revocation_writes_share_one_transaction(
        self, disconnect_db, monkeypatch
    ):
        monkeypatch.setattr(
            "src.server.database.mcp_oauth.get_connection",
            _connected(_project(_row(), Secrets.NONE)),
        )

        assert await lifecycle.disconnect_server(USER_ID, SERVER_NAME) is True

        # Same connection, transaction open, for each of the three.
        assert disconnect_db.trace[:3] == [
            ("mark_status", True, 1),
            ("revoke_grants", True, 1),
            ("delete_schemas", True, 1),
        ]
        # The fan-out is convergence across every workspace of the user, not
        # part of the revoke — it commits on its own, after.
        assert disconnect_db.trace[3:] == [("bump_versions", False, 0)]

    async def test_no_connection_writes_nothing(self, disconnect_db, monkeypatch):
        monkeypatch.setattr(
            "src.server.database.mcp_oauth.get_connection", _connected(None)
        )

        assert await lifecycle.disconnect_server(USER_ID, SERVER_NAME) is False
        assert disconnect_db.writes == []
