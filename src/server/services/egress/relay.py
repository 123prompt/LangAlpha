"""The egress relay's per-request pipeline: authenticate → authorize → attach
the vendor bearer → stream the exchange through.

Stateless per request (``--workers N`` free): the sandbox proves itself with a
relay JWT, the grant row authorizes exactly one destination captured at grant
creation, and the vendor access token is read fresh from Postgres each call —
rotation and revocation are instant by construction, with zero sandbox
convergence.

Header discipline is allowlist-both-ways: the sandbox's relay Authorization
never reaches the vendor; the vendor's Set-Cookie / WWW-Authenticate never
reach the sandbox.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from src.config.env import EGRESS_RELAY_SECRET
from src.server.database.egress_grants import fetch_grant_for_relay
from src.server.services.egress import RelayError
from src.server.services.egress.jsonrpc import (
    CanonicalRequest,
    JsonRpcRejected,
    canonicalize_request,
)
from src.server.services.egress.relay_jwt import (
    RelayClaims,
    RelayJwtError,
    validate_relay_jwt,
)
from src.server.utils.egress_guard import EgressBlockedError, pin_public_url

if TYPE_CHECKING:
    from src.server.services.mcp_oauth.lifecycle import AccessToken

logger = logging.getLogger(__name__)

# Timeout ladder (spec §D): the read timeout is per-chunk idle, not total —
# the router enforces the 55s wall clock around the whole exchange.
CONNECT_TIMEOUT_S = 5.0
WRITE_TIMEOUT_S = 10.0
READ_IDLE_TIMEOUT_S = 45.0
WALL_CLOCK_S = 55.0

# Connection pool. httpx defaults keepalive_expiry to 5s, which is shorter than
# the model latency between two execute_code blocks — so every burst of MCP
# calls would re-pay a TCP+TLS handshake (~2 RTT) to the vendor. Holding idle
# connections across a turn is the whole point of pooling here.
KEEPALIVE_EXPIRY_S = 300.0
MAX_KEEPALIVE_CONNECTIONS = 40
MAX_UPSTREAM_CONNECTIONS = 200

# Sandbox → vendor: only what the generated MCP client legitimately sends.
# mcp-method / mcp-name carry the 2026-07-28 stateless negotiation (server/
# discover, per-call routing); without them a modern server can't negotiate
# through the relay and every OAuth connector silently pins to the legacy
# handshake.
REQUEST_HEADER_ALLOWLIST = frozenset(
    {
        "accept",
        "content-type",
        "mcp-protocol-version",
        "mcp-session-id",
        "mcp-method",
        "mcp-name",
    }
)
# Vendor → sandbox: transport essentials plus the vendor's backoff hint.
RESPONSE_HEADER_ALLOWLIST = frozenset(
    {"content-type", "mcp-session-id", "mcp-protocol-version", "retry-after"}
)


class RelayRejection(Exception):
    """Terminal per-request outcome, mapped by the router to an HTTP answer.

    ``code`` is machine-readable and surfaced as an X-Relay-Error header so
    the generated client (and the agent) can distinguish relay-auth failures
    from vendor-auth failures without parsing bodies.
    """

    def __init__(self, status: int, code: RelayError, detail: str = ""):
        self.status = status
        self.code = RelayError(code)
        self.detail = detail or str(self.code)
        super().__init__(self.detail)


@dataclass
class PreparedRelay:
    claims: RelayClaims
    grant: dict
    canonical: CanonicalRequest
    token: AccessToken


_client: httpx.AsyncClient | None = None


def get_relay_client() -> httpx.AsyncClient:
    """Lazy per-worker upstream client; httpx pools by origin internally."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            http2=True,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT_S,
                write=WRITE_TIMEOUT_S,
                read=READ_IDLE_TIMEOUT_S,
                pool=CONNECT_TIMEOUT_S,
            ),
            limits=httpx.Limits(
                max_connections=MAX_UPSTREAM_CONNECTIONS,
                max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
                keepalive_expiry=KEEPALIVE_EXPIRY_S,
            ),
        )
    return _client


async def close_relay_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def authenticate_relay(authorization: str | None) -> RelayClaims:
    """Validate the sandbox's relay JWT — no body required.

    Callers authenticate BEFORE buffering the request body, so an
    unauthenticated client can never stream an arbitrary payload into worker
    memory (the grant lookup and body read both come after this returns).
    """
    if not EGRESS_RELAY_SECRET:
        raise RelayRejection(503, RelayError.RELAY_DISABLED)
    if not authorization or not authorization.lower().startswith("bearer "):
        raise RelayRejection(401, RelayError.RELAY_AUTH)
    try:
        return validate_relay_jwt(
            EGRESS_RELAY_SECRET, authorization.split(" ", 1)[1].strip()
        )
    except RelayJwtError:
        raise RelayRejection(401, RelayError.RELAY_AUTH)


async def prepare_relay(
    grant_id: str,
    *,
    claims: RelayClaims,
    raw_body: bytes,
) -> PreparedRelay:
    """Authorize the grant + ready the vendor token (sandbox already authed)."""
    try:
        canonical = canonicalize_request(raw_body)
    except JsonRpcRejected as e:
        raise RelayRejection(400, RelayError.BAD_REQUEST, str(e))

    grant = await fetch_grant_for_relay(grant_id)
    # Absent, revoked, and wrong-scope all answer the same 404 — the relay is
    # never an oracle for other users' grant ids. claims.sandbox_id is carried
    # for audit only, not authorized against: workspace↔sandbox is 1:1, so a
    # stale sandbox's JWT reaches exactly the same grants its workspace owns.
    if (
        grant is None
        or grant["grant_status"] != "active"
        or grant["workspace_id"] != claims.workspace_id
        or grant["user_id"] != claims.user_id
    ):
        raise RelayRejection(404, RelayError.NOT_FOUND)

    from src.server.services.mcp_oauth import SERVABLE

    if grant["connection_status"] not in SERVABLE:
        raise RelayRejection(401, RelayError.NEEDS_REAUTH)

    # HTTP-verb grant policy (defaults to ["POST"]). The route is POST-only
    # today, so this bites only when a grant is deliberately narrowed to [].
    if "POST" not in (grant.get("allowed_methods") or []):
        raise RelayRejection(403, RelayError.METHOD_BLOCKED, "POST not in grant policy")

    allowlist = grant.get("tool_allowlist")
    if (
        allowlist is not None
        and canonical.method == "tools/call"
        and canonical.tool_name not in allowlist
    ):
        raise RelayRejection(403, RelayError.TOOL_BLOCKED, "tool not in grant policy")

    from src.server.services.mcp_oauth.lifecycle import (
        TokenUnavailable,
        ensure_fresh_access_token,
    )

    try:
        token = await ensure_fresh_access_token(grant["connection_id"])
    except TokenUnavailable as e:
        if e.reason == "refresh_in_progress":
            raise RelayRejection(503, RelayError.REFRESH_IN_PROGRESS)
        raise RelayRejection(401, RelayError.NEEDS_REAUTH, e.reason)

    return PreparedRelay(
        claims=claims, grant=grant, canonical=canonical, token=token
    )


def _vendor_headers(
    prepared: PreparedRelay, incoming: dict[str, str]
) -> dict[str, str]:
    # Keys normalized to lowercase: a case-preserving copy plus a title-case
    # setdefault would put the same header on the wire twice.
    headers = {
        k.lower(): v
        for k, v in incoming.items()
        if k.lower() in REQUEST_HEADER_ALLOWLIST
    }
    headers.setdefault("accept", "application/json, text/event-stream")
    headers.setdefault("content-type", "application/json")
    headers["authorization"] = prepared.token.header()
    return headers


async def open_upstream(
    prepared: PreparedRelay, incoming_headers: dict[str, str]
) -> httpx.Response:
    """Send the canonical body to the pinned destination; one 401 retry when
    the stored bundle has visibly rotated since we read it."""
    destination = prepared.grant["destination_url"]
    try:
        target = await pin_public_url(destination, require_https=True)
    except EgressBlockedError as e:
        # The destination was validated at grant creation; a failure here is
        # DNS trouble or a rebinding attempt — refuse, never resolve privately.
        # The reason (which names the vendor host and is a DNS-resolution
        # oracle) stays host-side; the sandbox gets only the X-Relay-Error code.
        logger.warning(
            "[egress_relay] destination pin failed for connection %s: %s",
            prepared.grant["connection_id"], e,
        )
        raise RelayRejection(502, RelayError.DESTINATION_BLOCKED)

    client = get_relay_client()
    headers = _vendor_headers(prepared, incoming_headers)
    headers["Host"] = target.authority

    async def _send(hdrs: dict[str, str]) -> httpx.Response:
        request = client.build_request(
            "POST",
            target.url,
            headers=hdrs,
            content=prepared.canonical.body,
            extensions={"sni_hostname": target.host},
        )
        return await client.send(request, stream=True)

    try:
        response = await _send(headers)
    except httpx.HTTPError as e:
        logger.warning(
            "[egress_relay] upstream unreachable for connection %s: %s",
            prepared.grant["connection_id"], e,
        )
        raise RelayRejection(502, RelayError.UPSTREAM_UNREACHABLE)

    if response.status_code != 401:
        return response

    # Vendor 401: disambiguate a stale-token race from a dead grant. If the
    # stored bundle rotated since our read, retry once with the new token;
    # otherwise the vendor is rejecting a current token → needs_reauth.
    await response.aclose()
    from src.server.services.mcp_oauth.lifecycle import (
        current_access_token,
        mark_connection_needs_reauth,
    )

    connection_id = prepared.grant["connection_id"]
    rejected = prepared.token
    current = await current_access_token(connection_id)
    if current is not None and current.generation > rejected.generation:
        headers["authorization"] = current.header()
        try:
            retry = await _send(headers)
        except httpx.HTTPError as e:
            logger.warning(
                "[egress_relay] upstream unreachable on retry for connection %s: %s",
                prepared.grant["connection_id"], e,
            )
            raise RelayRejection(502, RelayError.UPSTREAM_UNREACHABLE)
        if retry.status_code != 401:
            return retry
        await retry.aclose()
        rejected = current
    # The connection owns its own status: this only reports which bundle the
    # vendor turned down, and a rotation since then makes that report moot.
    await mark_connection_needs_reauth(
        connection_id, seen_token_generation=rejected.generation
    )
    raise RelayRejection(401, RelayError.NEEDS_REAUTH, "vendor rejected the token")


def sandbox_response_headers(upstream: httpx.Response) -> dict[str, str]:
    headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() in RESPONSE_HEADER_ALLOWLIST
    }
    content_type = headers.get("Content-Type") or headers.get("content-type") or ""
    if content_type.startswith("text/event-stream"):
        # Match the app's SSE routes; GZip auto-exempts event-stream already.
        headers["Cache-Control"] = "no-cache, no-transform"
        headers["X-Accel-Buffering"] = "no"
    return headers
