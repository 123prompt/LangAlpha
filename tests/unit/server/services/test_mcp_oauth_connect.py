"""Unit tests for the two-phase MCP OAuth connect flow.

Phase 1 (``start_connect``) parks a state + PKCE record in Redis and hands the
browser an authorize URL; phase 2 (``complete_callback``) claims that record,
exchanges the code, and answers with a relative redirect. Three properties
carry the flow's safety, and each gets its own coverage here:

- the state record is **claimed exactly once** — the claim is get-and-delete in
  one MULTI/EXEC step, so a replayed (or concurrent) callback loses;
- the PKCE verifier parked in phase 1 is the one presented at the token
  endpoint, and the authorize URL carries its S256 challenge (recomputed here
  rather than read back from the implementation);
- every user-visible outcome lands on an allowlisted **relative** path, with
  the exact ``?mcp_error=`` / ``?mcp_connected=`` vocabulary the UI reads.

Every network seam (discovery, DCR, token exchange) and the SSRF pin is
monkeypatched: nothing here touches Redis, Postgres, or the network.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata

from src.server.services.mcp_oauth import connect
from src.server.services.mcp_oauth.connect import (
    DEFAULT_RETURN_TO,
    STATE_TTL_SECONDS,
    McpOAuthError,
    complete_callback,
    sanitize_return_to,
    start_connect,
)
from src.server.services.mcp_oauth.http import OAuthHopBlocked
from src.server.utils.egress_guard import EgressBlockedError, PinnedTarget

USER_ID = "user-connect-1"
# The space is deliberate: it proves the redirect percent-encodes the name.
SERVER_NAME = "demo notes"
SERVER_NAME_Q = "demo%20notes"
SERVER_URL = "https://mcp.demo.test/mcp"
ISSUER = "https://auth.demo.test"
AUTH_HOST = "auth.demo.test"
STATE_PREFIX = "mcp:oauth:state:"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakePipeline:
    """MULTI/EXEC stand-in: queued commands apply as one indivisible step.

    ``execute`` awaits once (the round trip a real client would make) and then
    applies every queued command with no further await point — which is
    precisely the property that makes the state claim single-use under
    concurrency.
    """

    def __init__(self, redis: "FakeRedis"):
        self._redis = redis
        self._queued: list[tuple[str, str]] = []

    async def __aenter__(self) -> "_FakePipeline":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def get(self, key: str) -> "_FakePipeline":
        self._queued.append(("get", key))
        return self

    def delete(self, key: str) -> "_FakePipeline":
        self._queued.append(("delete", key))
        return self

    async def execute(self) -> list:
        queued, self._queued = self._queued, []
        await asyncio.sleep(0)
        results: list = []
        for op, key in queued:
            if op == "get":
                results.append(self._redis.store.get(key))
            else:
                results.append(int(self._redis.store.pop(key, None) is not None))
        return results


class FakeRedis:
    def __init__(self, *, nx_always_loses: bool = False):
        self.store: dict[str, str] = {}
        self.set_calls: list[dict] = []
        self._nx_always_loses = nx_always_loses

    async def set(self, key, value, *, nx=False, ex=None):
        await asyncio.sleep(0)
        self.set_calls.append({"key": key, "nx": nx, "ex": ex})
        if nx and (self._nx_always_loses or key in self.store):
            return None
        self.store[key] = value
        return True

    def pipeline(self, transaction=True):
        assert transaction, "the state claim must run inside MULTI/EXEC"
        return _FakePipeline(self)

    # -- test helpers -------------------------------------------------------

    def only_record(self) -> dict:
        [raw] = list(self.store.values())
        return json.loads(raw)

    def park(self, state: str, record: dict) -> None:
        self.store[f"{STATE_PREFIX}{state}"] = json.dumps(record)


@asynccontextmanager
async def _fake_http_client():
    yield SimpleNamespace(name="fake-oauth-client")


def _as_metadata(**overrides) -> OAuthMetadata:
    data = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "registration_endpoint": f"{ISSUER}/register",
        "scopes_supported": ["notes.read", "offline_access"],
        "code_challenge_methods_supported": ["S256"],
    }
    data.update(overrides)
    return OAuthMetadata.model_validate(data)


def _client_info(**overrides) -> OAuthClientInformationFull:
    data = {
        "client_id": "client-abc123",
        "client_name": "Langalpha",
        "redirect_uris": [connect._redirect_uri()],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    data.update(overrides)
    return OAuthClientInformationFull.model_validate(data)


def _token_payload(**overrides) -> dict:
    payload = {
        "access_token": "access-fresh",
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": "refresh-fresh",
        "scope": "notes.read offline_access",
    }
    payload.update(overrides)
    return payload


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def redis(monkeypatch) -> FakeRedis:
    fake = FakeRedis()
    cache = SimpleNamespace(enabled=True, client=fake)
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client", lambda: cache
    )
    return fake


@pytest.fixture
def phase1(monkeypatch) -> SimpleNamespace:
    """Patch every phase-1 seam; the returned handle tweaks the scenario."""
    env = SimpleNamespace(
        as_metadata=_as_metadata(),
        client_info=_client_info(),
        prm=None,
        www_scope=None,
        catalog_row={"name": SERVER_NAME, "url": SERVER_URL, "transport": "http"},
        pinned=[],
        pin_error=None,
    )

    async def _discover(client, server_url):
        assert server_url == SERVER_URL
        return env.prm, env.as_metadata, ISSUER, env.www_scope

    async def _register_client(client, **kwargs):
        return env.client_info

    async def _get_catalog_server(user_id, name):
        return env.catalog_row

    async def _pin(url, *, require_https=True, allow_non_global=False):
        env.pinned.append({"url": url, "require_https": require_https})
        if env.pin_error is not None:
            raise env.pin_error
        return PinnedTarget(url=url, host=AUTH_HOST, ip="203.0.113.10")

    monkeypatch.setattr(connect, "_discover", _discover)
    monkeypatch.setattr(connect, "_register_client", _register_client)
    monkeypatch.setattr(connect, "get_catalog_server", _get_catalog_server)
    monkeypatch.setattr(connect, "oauth_http_client", _fake_http_client)
    monkeypatch.setattr("src.server.utils.egress_guard.pin_public_url", _pin)
    return env


@pytest.fixture
def phase2(monkeypatch) -> SimpleNamespace:
    """Patch every phase-2 seam; the returned handle records what was sent."""
    env = SimpleNamespace(
        requests=[],
        upserts=[],
        bumps=[],
        discoveries=[],
        status_code=200,
        payload=_token_payload(),
        raises=None,
        discovery_error=None,
    )

    async def _pinned_request(client, method, url, *, headers=None, data=None, content=None):
        env.requests.append(
            {"method": method, "url": url, "headers": headers, "data": data}
        )
        if env.raises is not None:
            raise env.raises
        return httpx2.Response(env.status_code, json=env.payload)

    async def _upsert_connection(user_id, server_name, **kwargs):
        env.upserts.append({"user_id": user_id, "server_name": server_name, **kwargs})
        return "connection-1"

    async def _bump(user_id):
        env.bumps.append(user_id)

    async def _refresh_user_tool_schemas(user_id, server_name):
        env.discoveries.append((user_id, server_name))
        if env.discovery_error is not None:
            raise env.discovery_error

    monkeypatch.setattr(connect, "pinned_request", _pinned_request)
    monkeypatch.setattr(connect, "oauth_http_client", _fake_http_client)
    monkeypatch.setattr(connect, "upsert_connection", _upsert_connection)
    monkeypatch.setattr(connect, "bump_user_workspaces_mcp_version", _bump)
    monkeypatch.setattr(
        "src.server.services.mcp_oauth.discovery.refresh_user_tool_schemas",
        _refresh_user_tool_schemas,
    )
    return env


# ---------------------------------------------------------------------------
# Phase 1 — the parked state record
# ---------------------------------------------------------------------------


class TestStartConnect:
    @pytest.mark.asyncio
    async def test_parks_a_single_use_ttl_bounded_state_record(self, redis, phase1):
        result = await start_connect(USER_ID, SERVER_NAME)

        [call] = redis.set_calls
        assert call["key"] == f"{STATE_PREFIX}{result['state']}"
        # nx: the state key is claimed, never overwritten. ex: it self-expires.
        assert call["nx"] is True
        assert call["ex"] == STATE_TTL_SECONDS

        record = redis.only_record()
        assert record["user_id"] == USER_ID
        assert record["server_name"] == SERVER_NAME
        assert record["server_url"] == SERVER_URL
        assert record["issuer"] == str(phase1.as_metadata.issuer)
        assert record["token_endpoint"] == str(phase1.as_metadata.token_endpoint)
        assert record["redirect_uri"] == connect._redirect_uri()
        assert record["return_to"] == DEFAULT_RETURN_TO

    @pytest.mark.asyncio
    async def test_authorize_url_carries_the_s256_challenge_of_the_parked_verifier(
        self, redis, phase1
    ):
        result = await start_connect(USER_ID, SERVER_NAME)

        params = _query(result["authorize_url"])
        verifier = redis.only_record()["code_verifier"]

        assert params["code_challenge_method"] == "S256"
        # Recomputed here — never read back from the implementation.
        assert params["code_challenge"] == _s256(verifier)
        assert params["code_challenge"] != verifier
        assert params["state"] == result["state"]
        assert params["response_type"] == "code"
        assert params["client_id"] == "client-abc123"
        assert params["redirect_uri"] == connect._redirect_uri()
        assert result["authorize_url"].startswith(f"{ISSUER}/authorize?")

    @pytest.mark.asyncio
    async def test_offline_access_asks_for_explicit_consent(self, redis, phase1):
        # AS scopes_supported carries offline_access, so the durable-grant
        # prompt must be requested.
        params = _query((await start_connect(USER_ID, SERVER_NAME))["authorize_url"])

        assert params["scope"] == "notes.read offline_access"
        assert params["prompt"] == "consent"

    @pytest.mark.asyncio
    async def test_no_consent_prompt_without_offline_access(self, redis, phase1):
        phase1.as_metadata = _as_metadata(scopes_supported=["notes.read"])

        params = _query((await start_connect(USER_ID, SERVER_NAME))["authorize_url"])

        assert params["scope"] == "notes.read"
        assert "prompt" not in params

    @pytest.mark.asyncio
    async def test_authorization_endpoint_is_pinned_public_https(self, redis, phase1):
        await start_connect(USER_ID, SERVER_NAME)

        assert phase1.pinned == [
            {"url": f"{ISSUER}/authorize", "require_https": True}
        ]

    @pytest.mark.asyncio
    async def test_refuses_a_non_public_authorization_endpoint(self, redis, phase1):
        phase1.pin_error = EgressBlockedError("resolves to a non-global address")

        with pytest.raises(McpOAuthError, match="Refusing authorization endpoint"):
            await start_connect(USER_ID, SERVER_NAME)

        # Nothing is parked for a flow that never produced an authorize URL.
        assert redis.store == {}

    @pytest.mark.asyncio
    async def test_state_collision_is_refused(self, monkeypatch, phase1):
        colliding = FakeRedis(nx_always_loses=True)
        cache = SimpleNamespace(enabled=True, client=colliding)
        monkeypatch.setattr(
            "src.utils.cache.redis_cache.get_cache_client", lambda: cache
        )

        with pytest.raises(McpOAuthError, match="state collision"):
            await start_connect(USER_ID, SERVER_NAME)

    @pytest.mark.asyncio
    async def test_requires_a_known_remote_http_server(self, redis, phase1):
        phase1.catalog_row = None
        with pytest.raises(McpOAuthError, match="not found"):
            await start_connect(USER_ID, SERVER_NAME)

        phase1.catalog_row = {"name": SERVER_NAME, "transport": "stdio", "url": None}
        with pytest.raises(McpOAuthError, match="remote"):
            await start_connect(USER_ID, SERVER_NAME)

        assert redis.store == {}


# ---------------------------------------------------------------------------
# Round trip — phase 1 parks, phase 2 consumes
# ---------------------------------------------------------------------------


class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_parked_verifier_is_the_one_presented_at_the_token_endpoint(
        self, redis, phase1, phase2
    ):
        started = await start_connect(USER_ID, SERVER_NAME)
        verifier = redis.only_record()["code_verifier"]
        challenge = _query(started["authorize_url"])["code_challenge"]

        redirect = await complete_callback(state=started["state"], code="auth-code-1")

        assert redirect == f"{DEFAULT_RETURN_TO}?mcp_connected={SERVER_NAME_Q}"
        [exchange] = phase2.requests
        assert exchange["method"] == "POST"
        assert exchange["url"] == f"{ISSUER}/token"
        assert exchange["data"]["grant_type"] == "authorization_code"
        assert exchange["data"]["code"] == "auth-code-1"
        assert exchange["data"]["client_id"] == "client-abc123"
        assert exchange["data"]["redirect_uri"] == connect._redirect_uri()
        # The pairing that makes PKCE worth anything.
        assert exchange["data"]["code_verifier"] == verifier
        assert _s256(exchange["data"]["code_verifier"]) == challenge

    @pytest.mark.asyncio
    async def test_success_persists_the_bundle_and_fans_out(
        self, redis, phase1, phase2
    ):
        started = await start_connect(USER_ID, SERVER_NAME)

        await complete_callback(state=started["state"], code="auth-code-1")

        [upsert] = phase2.upserts
        assert upsert["user_id"] == USER_ID
        assert upsert["server_name"] == SERVER_NAME
        assert upsert["server_url"] == SERVER_URL
        assert upsert["access_token"] == "access-fresh"
        assert upsert["refresh_token"] == "refresh-fresh"
        assert upsert["token_type"] == "Bearer"
        assert upsert["scope"] == "notes.read offline_access"
        expected = datetime.now(timezone.utc) + timedelta(seconds=3600)
        assert abs((upsert["expires_at"] - expected).total_seconds()) < 30
        # Sessions must re-resolve, and tools should appear immediately.
        assert phase2.bumps == [USER_ID]
        assert phase2.discoveries == [(USER_ID, SERVER_NAME)]

    @pytest.mark.asyncio
    async def test_post_connect_discovery_failure_still_connects(
        self, redis, phase1, phase2
    ):
        phase2.discovery_error = RuntimeError("server hung up during tools/list")
        started = await start_connect(USER_ID, SERVER_NAME)

        redirect = await complete_callback(state=started["state"], code="auth-code-1")

        assert redirect == f"{DEFAULT_RETURN_TO}?mcp_connected={SERVER_NAME_Q}"
        assert phase2.upserts, "the connection is stored before discovery is attempted"

    @pytest.mark.asyncio
    async def test_matching_iss_is_accepted(self, redis, phase1, phase2):
        started = await start_connect(USER_ID, SERVER_NAME)
        issuer = redis.only_record()["issuer"]

        redirect = await complete_callback(
            state=started["state"], code="auth-code-1", iss=issuer
        )

        assert redirect == f"{DEFAULT_RETURN_TO}?mcp_connected={SERVER_NAME_Q}"


# ---------------------------------------------------------------------------
# Single-use state claim
# ---------------------------------------------------------------------------


class TestSingleUseState:
    @pytest.mark.asyncio
    async def test_a_replayed_state_is_rejected(self, redis, phase1, phase2):
        started = await start_connect(USER_ID, SERVER_NAME)

        first = await complete_callback(state=started["state"], code="auth-code-1")
        second = await complete_callback(state=started["state"], code="auth-code-1")

        assert first == f"{DEFAULT_RETURN_TO}?mcp_connected={SERVER_NAME_Q}"
        assert second == f"{DEFAULT_RETURN_TO}?mcp_error=invalid_state"
        # The replay never reaches the token endpoint.
        assert len(phase2.requests) == 1
        assert len(phase2.upserts) == 1
        assert redis.store == {}

    @pytest.mark.asyncio
    async def test_concurrent_callbacks_claim_the_state_once(
        self, redis, phase1, phase2
    ):
        started = await start_connect(USER_ID, SERVER_NAME)

        results = await asyncio.gather(
            complete_callback(state=started["state"], code="auth-code-1"),
            complete_callback(state=started["state"], code="auth-code-1"),
        )

        assert sorted(results) == sorted(
            [
                f"{DEFAULT_RETURN_TO}?mcp_connected={SERVER_NAME_Q}",
                f"{DEFAULT_RETURN_TO}?mcp_error=invalid_state",
            ]
        )
        assert len(phase2.requests) == 1

    @pytest.mark.asyncio
    async def test_unknown_state_is_indistinguishable_from_a_used_one(
        self, redis, phase2
    ):
        redirect = await complete_callback(state="never-issued", code="auth-code-1")

        # Same answer as a replay, on the default path: no oracle for whether a
        # state ever existed, and no parked return_to to consult.
        assert redirect == f"{DEFAULT_RETURN_TO}?mcp_error=invalid_state"
        assert phase2.requests == []


# ---------------------------------------------------------------------------
# Callback error arms
# ---------------------------------------------------------------------------


class TestCallbackErrors:
    @pytest.mark.asyncio
    async def test_missing_state(self, redis, phase2):
        assert await complete_callback(state=None, code="x") == (
            f"{DEFAULT_RETURN_TO}?mcp_error=missing_state"
        )
        assert await complete_callback(state="", code="x") == (
            f"{DEFAULT_RETURN_TO}?mcp_error=missing_state"
        )
        assert phase2.requests == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("error", "reason"),
        [
            ("access_denied", "denied"),
            ("server_error", "provider_error"),
            ("invalid_scope", "provider_error"),
        ],
    )
    async def test_authorization_server_error(
        self, redis, phase1, phase2, error, reason
    ):
        started = await start_connect(USER_ID, SERVER_NAME)

        redirect = await complete_callback(
            state=started["state"],
            code=None,
            error=error,
            error_description="user cancelled",
        )

        assert redirect == (
            f"{DEFAULT_RETURN_TO}?mcp_error={reason}&server={SERVER_NAME_Q}"
        )
        assert phase2.requests == []
        # A failed callback still burns the state.
        assert redis.store == {}

    @pytest.mark.asyncio
    async def test_missing_code(self, redis, phase1, phase2):
        started = await start_connect(USER_ID, SERVER_NAME)

        redirect = await complete_callback(state=started["state"], code=None)

        assert redirect == (
            f"{DEFAULT_RETURN_TO}?mcp_error=missing_code&server={SERVER_NAME_Q}"
        )
        assert phase2.requests == []

    @pytest.mark.asyncio
    async def test_issuer_mismatch(self, redis, phase1, phase2):
        started = await start_connect(USER_ID, SERVER_NAME)

        redirect = await complete_callback(
            state=started["state"], code="auth-code-1", iss="https://evil.test/"
        )

        assert redirect == (
            f"{DEFAULT_RETURN_TO}?mcp_error=issuer_mismatch&server={SERVER_NAME_Q}"
        )
        assert phase2.requests == []

    @pytest.mark.asyncio
    async def test_token_exchange_rejected(self, redis, phase1, phase2):
        phase2.status_code = 400
        phase2.payload = {"error": "invalid_grant"}
        started = await start_connect(USER_ID, SERVER_NAME)

        redirect = await complete_callback(state=started["state"], code="auth-code-1")

        assert redirect == (
            f"{DEFAULT_RETURN_TO}?mcp_error=token_exchange_failed"
            f"&server={SERVER_NAME_Q}"
        )
        assert phase2.upserts == []

    @pytest.mark.asyncio
    async def test_token_exchange_transport_error(self, redis, phase1, phase2):
        phase2.raises = httpx2.ConnectError("connection reset")
        started = await start_connect(USER_ID, SERVER_NAME)

        redirect = await complete_callback(state=started["state"], code="auth-code-1")

        assert redirect == (
            f"{DEFAULT_RETURN_TO}?mcp_error=token_exchange_failed"
            f"&server={SERVER_NAME_Q}"
        )
        assert phase2.upserts == []

    @pytest.mark.asyncio
    async def test_blocked_token_endpoint(self, redis, phase1, phase2):
        phase2.raises = OAuthHopBlocked("egress to token endpoint is blocked")
        started = await start_connect(USER_ID, SERVER_NAME)

        redirect = await complete_callback(state=started["state"], code="auth-code-1")

        assert redirect == (
            f"{DEFAULT_RETURN_TO}?mcp_error=blocked_endpoint&server={SERVER_NAME_Q}"
        )
        assert phase2.upserts == []


# ---------------------------------------------------------------------------
# return_to allowlisting
# ---------------------------------------------------------------------------


class TestReturnToAllowlist:
    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "//evil.test/phish",
            "https://evil.test/phish",
            "http://evil.test",
            "evil.test",
            "connectors",
            "\\\\evil.test",
        ],
    )
    def test_off_allowlist_values_fall_back_to_the_default(self, value):
        assert sanitize_return_to(value) == DEFAULT_RETURN_TO

    @pytest.mark.parametrize(
        "value", ["/connectors", "/settings/connectors", "/connectors?tab=oauth"]
    )
    def test_same_app_relative_paths_are_honored(self, value):
        assert sanitize_return_to(value) == value

    @pytest.mark.asyncio
    async def test_phase1_parks_only_the_sanitized_path(self, redis, phase1):
        await start_connect(USER_ID, SERVER_NAME, return_to="https://evil.test/phish")

        assert redis.only_record()["return_to"] == DEFAULT_RETURN_TO

    @pytest.mark.asyncio
    async def test_honored_path_survives_to_the_success_redirect(
        self, redis, phase1, phase2
    ):
        started = await start_connect(
            USER_ID, SERVER_NAME, return_to="/settings/connectors"
        )

        redirect = await complete_callback(state=started["state"], code="auth-code-1")

        assert redirect == f"/settings/connectors?mcp_connected={SERVER_NAME_Q}"

    @pytest.mark.asyncio
    async def test_phase2_resanitizes_a_poisoned_record(self, redis, phase1, phase2):
        """Defense in depth: even a record whose return_to bypassed phase 1
        cannot steer the browser off-app."""
        started = await start_connect(USER_ID, SERVER_NAME)
        record = redis.only_record()
        record["return_to"] = "https://evil.test/phish"
        redis.store.clear()
        redis.park(started["state"], record)

        redirect = await complete_callback(state=started["state"], code="auth-code-1")

        assert redirect == f"{DEFAULT_RETURN_TO}?mcp_connected={SERVER_NAME_Q}"

    @pytest.mark.asyncio
    async def test_poisoned_record_cannot_steer_the_error_redirect_either(
        self, redis, phase1, phase2
    ):
        started = await start_connect(USER_ID, SERVER_NAME)
        record = redis.only_record()
        record["return_to"] = "//evil.test/phish"
        redis.store.clear()
        redis.park(started["state"], record)

        redirect = await complete_callback(
            state=started["state"], code=None, error="access_denied"
        )

        assert redirect.startswith(f"{DEFAULT_RETURN_TO}?mcp_error=denied")
