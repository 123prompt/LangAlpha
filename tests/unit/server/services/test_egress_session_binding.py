"""The egress session binding under multi-worker truth rules.

Two properties matter beyond the happy path. Removal must converge on a worker
that never bound anything — ``Session`` state is process-local, so the decision
to tear down the sandbox credential file comes from the ``sandbox_egress_grants``
table, not from ``session.egress_binding``. And mint+upload takes no cross-worker
lock: the credential file is replaced atomically inside the sandbox, so a
concurrent push can only overwrite this one's file with an equally-valid one —
never tear it. The push must therefore touch no DB connection at all.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.egress.session_binding import (
    EgressBinding,
    maybe_remint_egress_jwt,
    sync_egress_relay,
)

WS = "33333333-3333-4333-8333-333333333333"
USER = "user-1"
SECRET = "test-relay-secret-0123456789abcdef0123456789abcdef"


def _server(name: str, connection_id: str | None):
    return SimpleNamespace(
        name=name,
        oauth_connection_id=connection_id,
        url=f"https://vendor.example.test/{name}",
        egress_grant_id=None,
    )


def _session(binding: EgressBinding | None = None):
    sandbox = SimpleNamespace(
        sandbox_id="sb-1",
        # Confirmed-publication contract: True = the sandbox got the file.
        upload_egress_relay_credentials=AsyncMock(return_value=True),
    )
    return SimpleNamespace(
        sandbox=sandbox,
        egress_binding=binding,
        config=SimpleNamespace(sandbox=SimpleNamespace(provider="docker")),
    )


def _resolved(*servers):
    return SimpleNamespace(servers=list(servers))


@pytest.fixture
def secret():
    with patch("src.config.env.EGRESS_RELAY_SECRET", SECRET):
        yield


@pytest.fixture
def relay_base():
    with (
        patch(
            "src.server.services.egress.reachability.effective_relay_base_url",
            return_value="https://relay.example.test/",
        ),
        patch(
            "src.server.services.egress.reachability.relay_reachability_warning",
            return_value=None,
        ),
    ):
        yield


# ---------------------------------------------------------------------------
# Removal converges from the table, not from process-local session state.
# ---------------------------------------------------------------------------


class TestTeardown:
    @pytest.mark.asyncio
    async def test_fresh_worker_still_tears_down_when_the_table_has_grants(self):
        # Worker B: brand-new session (binding None), but the table says this
        # workspace has active grants — the credential file must still go.
        session = _session(binding=None)
        with patch(
            "src.server.services.egress.session_binding.retire_stale_grants",
            AsyncMock(return_value=2),
        ) as retire:
            await sync_egress_relay(WS, USER, session, _resolved())

        retire.assert_awaited_once_with(WS, keep_grant_ids=())
        session.sandbox.upload_egress_relay_credentials.assert_awaited_once_with(None)
        assert session.egress_binding is None

    @pytest.mark.asyncio
    async def test_no_grants_anywhere_uploads_nothing(self):
        # The common case (workspace never had OAuth servers): one indexed
        # no-op UPDATE, zero sandbox I/O.
        session = _session(binding=None)
        with patch(
            "src.server.services.egress.session_binding.retire_stale_grants",
            AsyncMock(return_value=0),
        ):
            await sync_egress_relay(WS, USER, session, _resolved())

        session.sandbox.upload_egress_relay_credentials.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_local_binding_is_cleared_even_when_the_table_is_already_clean(self):
        # Worker A raced: another worker retired the rows first — this
        # process's file copy and binding still converge.
        binding = EgressBinding(grants={"srv": "g1"}, jwt_exp=9e9, user_id=USER)
        session = _session(binding=binding)
        with patch(
            "src.server.services.egress.session_binding.retire_stale_grants",
            AsyncMock(return_value=0),
        ):
            await sync_egress_relay(WS, USER, session, _resolved())

        session.sandbox.upload_egress_relay_credentials.assert_awaited_once_with(None)
        assert session.egress_binding is None


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------


class TestBind:
    @pytest.mark.asyncio
    async def test_binds_grants_retires_stale_and_records_the_minted_expiry(
        self, secret, relay_base
    ):
        a, b = _server("srv_a", "conn-a"), _server("srv_b", "conn-b")
        session = _session()
        ensure = AsyncMock(side_effect=["grant-a", "grant-b"])
        retire = AsyncMock(return_value=0)
        with (
            patch(
                "src.server.services.egress.session_binding.ensure_oauth_grant",
                ensure,
            ),
            patch(
                "src.server.services.egress.session_binding.retire_stale_grants",
                retire,
            ),
        ):
            await sync_egress_relay(WS, USER, session, _resolved(a, b))

        assert a.egress_grant_id == "grant-a"
        assert b.egress_grant_id == "grant-b"
        retire.assert_awaited_once_with(WS, keep_grant_ids=("grant-a", "grant-b"))

        payload = session.sandbox.upload_egress_relay_credentials.await_args.args[0]
        assert payload["grants"] == {"srv_a": "grant-a", "srv_b": "grant-b"}
        assert payload["relay_base_url"] == "https://relay.example.test"
        assert payload["token"]

        binding = session.egress_binding
        assert binding.grants == {"srv_a": "grant-a", "srv_b": "grant-b"}
        assert binding.user_id == USER
        # The expiry comes from the mint itself, not a recompute.
        from src.server.services.egress.relay_jwt import validate_relay_jwt

        assert binding.jwt_exp == validate_relay_jwt(SECRET, payload["token"]).expires_at

    @pytest.mark.asyncio
    async def test_vanished_connection_leaves_only_that_server_unbound(
        self, secret, relay_base
    ):
        from src.server.database.egress_grants import GrantConnectionUnavailable

        gone, alive = _server("gone", "conn-gone"), _server("alive", "conn-ok")
        session = _session()
        ensure = AsyncMock(
            side_effect=[GrantConnectionUnavailable("gone"), "grant-ok"]
        )
        with (
            patch(
                "src.server.services.egress.session_binding.ensure_oauth_grant",
                ensure,
            ),
            patch(
                "src.server.services.egress.session_binding.retire_stale_grants",
                AsyncMock(return_value=0),
            ),
        ):
            await sync_egress_relay(WS, USER, session, _resolved(gone, alive))

        assert gone.egress_grant_id is None
        assert alive.egress_grant_id == "grant-ok"
        payload = session.sandbox.upload_egress_relay_credentials.await_args.args[0]
        assert payload["grants"] == {"alive": "grant-ok"}

    @pytest.mark.asyncio
    async def test_push_takes_no_db_connection(self, secret, relay_base):
        # The push no longer serializes on an advisory lock — atomic replace in
        # the sandbox makes concurrent workers safe — so it must not check out a
        # pooled connection and hold it across the (slow) sandbox upload. There
        # is no ``get_db_connection`` symbol left to patch; importing it here
        # asserts the module dropped the dependency entirely.
        import src.server.services.egress.session_binding as sb

        assert not hasattr(sb, "get_db_connection")

        srv = _server("srv", "conn-1")
        session = _session()
        with (
            patch(
                "src.server.services.egress.session_binding.ensure_oauth_grant",
                AsyncMock(return_value="g-1"),
            ),
            patch(
                "src.server.services.egress.session_binding.retire_stale_grants",
                AsyncMock(return_value=0),
            ),
        ):
            await sync_egress_relay(WS, USER, session, _resolved(srv))

        session.sandbox.upload_egress_relay_credentials.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failed_publish_leaves_the_binding_unbound(
        self, secret, relay_base
    ):
        # A publish that the sandbox didn't confirm must NOT advance the
        # binding — otherwise the process trusts a token the sandbox never got
        # and won't remint until that phantom token nears expiry.
        srv = _server("srv", "conn-1")
        session = _session()
        session.sandbox.upload_egress_relay_credentials = AsyncMock(return_value=False)
        with (
            patch(
                "src.server.services.egress.session_binding.ensure_oauth_grant",
                AsyncMock(return_value="g-1"),
            ),
            patch(
                "src.server.services.egress.session_binding.retire_stale_grants",
                AsyncMock(return_value=0),
            ),
        ):
            await sync_egress_relay(WS, USER, session, _resolved(srv))

        assert session.egress_binding is None


# ---------------------------------------------------------------------------
# Remint (warm fast path)
# ---------------------------------------------------------------------------


class TestRemint:
    @pytest.mark.asyncio
    async def test_noop_without_a_binding(self):
        session = _session(binding=None)
        await maybe_remint_egress_jwt(WS, session)
        session.sandbox.upload_egress_relay_credentials.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_noop_while_the_jwt_is_fresh(self):
        binding = EgressBinding(grants={"s": "g"}, jwt_exp=9e9, user_id=USER)
        session = _session(binding=binding)
        await maybe_remint_egress_jwt(WS, session)
        session.sandbox.upload_egress_relay_credentials.assert_not_awaited()
        assert session.egress_binding is binding

    @pytest.mark.asyncio
    async def test_near_expiry_repushes_with_the_bound_identity(
        self, secret, relay_base
    ):
        binding = EgressBinding(grants={"s": "g"}, jwt_exp=1.0, user_id=USER)
        session = _session(binding=binding)
        await maybe_remint_egress_jwt(WS, session)

        payload = session.sandbox.upload_egress_relay_credentials.await_args.args[0]
        assert payload["grants"] == {"s": "g"}
        refreshed = session.egress_binding
        assert refreshed is not binding
        assert refreshed.user_id == USER
        assert refreshed.jwt_exp > 1.0
