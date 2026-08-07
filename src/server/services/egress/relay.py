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

import httpx

from src.config.env import EGRESS_RELAY_SECRET
from src.server.database.egress_grants import fetch_grant_for_relay
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

logger = logging.getLogger(__name__)

# Timeout ladder (spec §D): the read timeout is per-chunk idle, not total —
# the router enforces the 55s wall clock around the whole exchange.
CONNECT_TIMEOUT_S = 5.0
WRITE_TIMEOUT_S = 10.0
READ_IDLE_TIMEOUT_S = 45.0
WALL_CLOCK_S = 55.0

# Sandbox → vendor: only what the generated MCP client legitimately sends.
REQUEST_HEADER_ALLOWLIST = frozenset(
    {"accept", "content-type", "mcp-protocol-version", "mcp-session-id"}
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

    def __init__(self, status: int, code: str, detail: str = ""):
        self.status = status
        self.code = code
        self.detail = detail or code
        super().__init__(self.detail)


@dataclass
class PreparedRelay:
    claims: RelayClaims
    grant: dict
    canonical: CanonicalRequest
    access_token: str
    token_type: str


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
        )
    return _client


async def close_relay_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def prepare_relay(
    grant_id: str,
    *,
    authorization: str | None,
    raw_body: bytes,
) -> PreparedRelay:
    """Authenticate the sandbox, authorize the grant, ready the vendor token."""
    if not EGRESS_RELAY_SECRET:
        raise RelayRejection(503, "relay_disabled")

    if not authorization or not authorization.lower().startswith("bearer "):
        raise RelayRejection(401, "relay_auth")
    try:
        claims = validate_relay_jwt(
            EGRESS_RELAY_SECRET, authorization.split(" ", 1)[1].strip()
        )
    except RelayJwtError:
        raise RelayRejection(401, "relay_auth")

    try:
        canonical = canonicalize_request(raw_body)
    except JsonRpcRejected as e:
        raise RelayRejection(400, "bad_request", str(e))

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
        raise RelayRejection(404, "not_found")

    if grant["connection_status"] not in ("connected", "refresh_ambiguous"):
        raise RelayRejection(401, "needs_reauth")

    # HTTP-verb grant policy (defaults to ["POST"]). The route is POST-only
    # today, so this bites only when a grant is deliberately narrowed to [].
    if "POST" not in (grant.get("allowed_methods") or []):
        raise RelayRejection(403, "method_blocked", "POST not in grant policy")

    allowlist = grant.get("tool_allowlist")
    if (
        allowlist is not None
        and canonical.method == "tools/call"
        and canonical.tool_name not in allowlist
    ):
        raise RelayRejection(403, "tool_blocked", "tool not in grant policy")

    from src.server.services.mcp_oauth.lifecycle import (
        TokenUnavailable,
        ensure_fresh_access_token,
    )

    try:
        token = await ensure_fresh_access_token(grant["connection_id"])
    except TokenUnavailable as e:
        if e.reason == "refresh_in_progress":
            raise RelayRejection(503, "refresh_in_progress")
        raise RelayRejection(401, "needs_reauth", e.reason)

    return PreparedRelay(
        claims=claims,
        grant=grant,
        canonical=canonical,
        access_token=token["access_token"],
        token_type=token.get("token_type") or "Bearer",
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
    headers["authorization"] = f"{prepared.token_type} {prepared.access_token}"
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
        raise RelayRejection(502, "destination_blocked", str(e))

    client = get_relay_client()
    headers = _vendor_headers(prepared, incoming_headers)
    headers["Host"] = target.host

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
        raise RelayRejection(502, "upstream_unreachable", str(e))

    if response.status_code != 401:
        return response

    # Vendor 401: disambiguate a stale-token race from a dead grant. If the
    # stored bundle rotated since our read, retry once with the new token;
    # otherwise the vendor is rejecting a current token → needs_reauth.
    await response.aclose()
    from src.server.database.mcp_oauth import get_connection_by_id, mark_status

    current = await get_connection_by_id(
        prepared.grant["connection_id"], decrypt=True
    )
    if (
        current is not None
        and current.get("access_token")
        and current["access_token"] != prepared.access_token
    ):
        headers["authorization"] = (
            f"{current.get('token_type') or 'Bearer'} {current['access_token']}"
        )
        try:
            retry = await _send(headers)
        except httpx.HTTPError as e:
            raise RelayRejection(502, "upstream_unreachable", str(e))
        if retry.status_code != 401:
            return retry
        await retry.aclose()
    if current is not None and current["status"] == "connected":
        await mark_status(prepared.grant["connection_id"], "needs_reauth")
        logger.warning(
            "[egress_relay] vendor rejected a current token; connection %s "
            "flipped to needs_reauth",
            prepared.grant["connection_id"],
        )
    raise RelayRejection(401, "needs_reauth", "vendor rejected the token")


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
