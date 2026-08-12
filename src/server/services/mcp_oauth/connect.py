"""Durable two-phase OAuth connect for user-level MCP servers.

Phase 1 (:func:`start_connect`, any worker): discovery + DCR via SDK helpers,
generate state + PKCE, persist the bridge record in Redis, return the
authorize URL. Phase 2 (:func:`complete_callback`, any worker): atomic
single-use claim of the state record, token exchange, encrypted bundle into
``user_mcp_oauth_connections``, best-effort host-side schema discovery.

Callback identity comes exclusively from ``state``; the post-connect redirect
is an allowlisted relative path.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx2
from pydantic import BaseModel, ValidationError

from mcp.client.auth import PKCEParameters
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_client_registration_request,
    extract_resource_metadata_from_www_auth,
    extract_scope_from_www_auth,
    get_client_metadata_scopes,
    handle_auth_metadata_response,
    handle_protected_resource_response,
    handle_registration_response,
    validate_authorization_response_iss,
    validate_metadata_issuer,
)
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    ProtectedResourceMetadata,
)

from src.server.database.mcp_oauth import Secrets, get_connection, upsert_connection
from src.server.database.mcp_servers import (
    bump_user_workspaces_mcp_version,
    get_catalog_server,
)
from src.server.services.mcp_oauth.http import (
    OAuthHopBlocked,
    oauth_http_client,
    pinned_request,
    pinned_send,
)
from src.server.services.mcp_oauth.redirects import (
    DEFAULT_RETURN_TO,
    callback_is_loopback,
    callback_uri,
    redirect_to,
    sanitize_return_to,
    sanitize_web_origin,
)
from src.server.services.mcp_oauth.tokens import (
    PROTOCOL_VERSION,
    TokenExchangeError,
    TokenFailure,
    build_context,
    exchange_token,
)

logger = logging.getLogger(__name__)

STATE_TTL_SECONDS = 600
_STATE_KEY_PREFIX = "mcp:oauth:state:"

CLIENT_NAME = "Langalpha"

# The MCP endpoint probe advertises a protocol version so servers answer with
# era-appropriate WWW-Authenticate hints.
_PROBE_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": PROTOCOL_VERSION,
}


class McpOAuthError(Exception):
    """A connect-flow step failed in a way the caller should surface."""


class McpServerNotFound(McpOAuthError):
    """No such server in this user's catalog — the router's 404."""


@dataclass(frozen=True, slots=True)
class StartedConnect:
    """Phase 1's result.

    ``browser_nonce`` is cookie-only: it must never reach a JSON body, which
    would defeat HttpOnly and hand it to any XSS on the page. Keeping the
    result a record rather than a dict makes that a projection the router
    performs explicitly instead of a convention it remembers.
    """

    authorize_url: str
    state: str
    browser_nonce: str


class ConnectState(BaseModel):
    """The bridge record phase 1 parks in Redis and phase 2 claims.

    It crosses worker — and, across a deploy, build — boundaries as JSON, so it
    is validated on the way back in: a truncated or older-shaped record must
    fail like an expired one, not KeyError partway through the token exchange.
    Field names are the wire format; do not rename them.

    Only the presentation fields carry defaults. Everything the exchange
    depends on (identity, the PKCE verifier, the endpoints) is required,
    because a record missing any of it cannot produce a correct token request.
    """

    user_id: str
    server_name: str
    server_url: str
    code_verifier: str
    redirect_uri: str
    token_endpoint: str
    issuer: str
    resource: str | None = None
    scope: str | None = None
    client_info: dict[str, Any]
    as_metadata: dict[str, Any]
    resource_metadata: dict[str, Any] | None = None
    return_to: str = DEFAULT_RETURN_TO
    web_origin: str = ""
    # DCR confidential-client secret, carried out-of-band from client_info so it
    # never lands in the plaintext client_info JSONB column at persist. Empty
    # for public clients. Phase 2 re-attaches it for the token exchange and
    # stores it in its own encrypted column.
    client_secret: str = ""
    # High-entropy value mirrored into an HttpOnly cookie on the initiating
    # browser; the callback must present it back. Binds the callback to the
    # browser that started the flow so a stolen (state, code) pair replayed in
    # a victim's browser has no matching cookie and is refused. Defaulted so an
    # older-shaped record still validates (its callback simply skips the check).
    browser_nonce: str = ""


def _cache_client():
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not (cache.enabled and cache.client):
        raise McpOAuthError("Redis is required for the OAuth connect flow")
    return cache.client


async def _try_hop(client, url: str) -> httpx2.Response | None:
    """One discovery GET; None when that URL is simply absent or unusable.

    A blocked hop is not a miss — it aborts discovery rather than falling
    through to the next candidate.
    """
    try:
        return await pinned_request(client, "GET", url, headers=_PROBE_HEADERS)
    except OAuthHopBlocked:
        raise
    except Exception as e:
        logger.info("[mcp_oauth] discovery hop %s failed: %s", url, e)
        return None


async def _discover(client, server_url: str) -> tuple[
    ProtectedResourceMetadata | None, OAuthMetadata, str | None, str | None
]:
    """Run 401-probe → PRM → AS-metadata discovery. Returns
    (prm, as_metadata, auth_server_url, www_scope)."""
    www_auth_url: str | None = None
    www_scope: str | None = None
    # The probe is a hint source only — discovery can proceed without it.
    probe = await _try_hop(client, server_url)
    if probe is not None and probe.status_code == 401:
        www_auth_url = extract_resource_metadata_from_www_auth(probe)
        www_scope = extract_scope_from_www_auth(probe)

    prm: ProtectedResourceMetadata | None = None
    for url in build_protected_resource_metadata_discovery_urls(
        www_auth_url, server_url
    ):
        resp = await _try_hop(client, url)
        if resp is None:
            continue
        prm = await handle_protected_resource_response(resp)
        if prm is not None:
            break

    auth_server_url = (
        str(prm.authorization_servers[0]) if prm and prm.authorization_servers
        else None
    )

    as_metadata: OAuthMetadata | None = None
    for url in build_oauth_authorization_server_metadata_discovery_urls(
        auth_server_url, server_url
    ):
        resp = await _try_hop(client, url)
        if resp is None:
            continue
        keep_trying, meta = await handle_auth_metadata_response(resp)
        if meta is not None:
            if auth_server_url:
                validate_metadata_issuer(meta, auth_server_url)
            as_metadata = meta
            break
        if not keep_trying:
            break

    if as_metadata is None:
        raise McpOAuthError(
            "No OAuth authorization server metadata found for this server "
            "(RFC 8414 discovery failed) — it may not support OAuth."
        )
    return prm, as_metadata, auth_server_url, www_scope


async def _register_client(
    client,
    *,
    user_id: str,
    server_name: str,
    as_metadata: OAuthMetadata,
    client_metadata: OAuthClientMetadata,
    auth_base_url: str,
) -> OAuthClientInformationFull:
    """Reuse the stored DCR registration when the issuer matches; else register."""
    existing = await get_connection(user_id, server_name, secrets=Secrets.FULL)
    if existing and existing.client_info:
        try:
            stored = OAuthClientInformationFull.model_validate(existing.client_info)
            if stored.client_id and existing.as_metadata.get("issuer") == str(
                as_metadata.issuer
            ):
                # client_secret is stored encrypted, outside the JSONB blob.
                stored.client_secret = existing.client_secret
                return stored
        except Exception:
            logger.info(
                "[mcp_oauth] stored client_info for %s unusable; re-registering",
                server_name,
            )
    if as_metadata.registration_endpoint is None:
        raise McpOAuthError(
            "The authorization server does not support Dynamic Client "
            "Registration; pre-registered clients are not supported yet."
        )
    request = create_client_registration_request(
        as_metadata, client_metadata, auth_base_url
    )
    response = await pinned_send(client, request)
    return await handle_registration_response(response)


async def start_connect(
    user_id: str,
    server_name: str,
    *,
    return_to: str | None = None,
    web_origin: str | None = None,
) -> StartedConnect:
    """Phase 1: discovery + DCR + state/PKCE persist."""
    row = await get_catalog_server(user_id, server_name)
    if row is None:
        raise McpServerNotFound("MCP server not found")
    server_url = row.get("url")
    # http only: the generated sandbox client rejects legacy `sse` transport
    # (it never implemented the real GET→endpoint-event→POST flow), so an
    # sse-bound OAuth connection could never be used through the relay.
    if row.get("transport") != "http" or not server_url:
        raise McpOAuthError("OAuth connect requires a remote (http) MCP server")

    redirect_uri = callback_uri()
    async with oauth_http_client() as client:
        prm, as_metadata, auth_server_url, www_scope = await _discover(
            client, server_url
        )

        scope = get_client_metadata_scopes(www_scope, prm, as_metadata)
        client_metadata = OAuthClientMetadata(
            client_name=CLIENT_NAME,
            redirect_uris=[redirect_uri],  # type: ignore[list-item]
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope=scope,
        )
        context = build_context(
            server_url,
            client_metadata=client_metadata,
            prm=prm,
            as_metadata=as_metadata,
            auth_server_url=auth_server_url,
        )
        client_info = await _register_client(
            client,
            user_id=user_id,
            server_name=server_name,
            as_metadata=as_metadata,
            client_metadata=client_metadata,
            auth_base_url=context.get_authorization_base_url(server_url),
        )

    # The authorize URL is opened by the user's browser, not by us — but it
    # must still be a public HTTPS endpoint, or the flow becomes an open
    # redirector into private address space.
    from src.server.utils.egress_guard import pin_public_url

    authorize_endpoint = str(as_metadata.authorization_endpoint)
    try:
        await pin_public_url(authorize_endpoint, require_https=True)
    except Exception as e:
        raise McpOAuthError(f"Refusing authorization endpoint: {e}")

    pkce = PKCEParameters.generate()
    state = secrets.token_urlsafe(32)
    # Empty on a loopback callback: the record then takes the same skip path as
    # a pre-control record, so the verification logic needs no dev branch.
    browser_nonce = "" if callback_is_loopback() else secrets.token_urlsafe(32)

    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_info.client_id or "",
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": pkce.code_challenge,
        "code_challenge_method": "S256",
    }
    include_resource = context.should_include_resource_param(
        context.protocol_version
    )
    if include_resource:
        params["resource"] = context.get_resource_url()
    effective_scope = client_info.scope or scope
    if effective_scope:
        params["scope"] = effective_scope
        # Refresh tokens hinge on offline_access for many providers; ask for
        # explicit consent so the grant is durable.
        if "offline_access" in effective_scope.split():
            params["prompt"] = "consent"

    record = ConnectState(
        user_id=user_id,
        server_name=server_name,
        server_url=server_url,
        code_verifier=pkce.code_verifier,
        redirect_uri=redirect_uri,
        token_endpoint=str(as_metadata.token_endpoint),
        issuer=str(as_metadata.issuer),
        resource=params.get("resource"),
        scope=effective_scope,
        # client_secret is excluded here and carried in its own field — see
        # ConnectState.client_secret. Keeping it out of this blob is what stops
        # a confidential secret from being written plaintext to the client_info
        # JSONB column when phase 2 persists the connection.
        client_info=client_info.model_dump(
            mode="json", exclude_none=True, exclude={"client_secret"}
        ),
        as_metadata=as_metadata.model_dump(mode="json", exclude_none=True),
        resource_metadata=(
            prm.model_dump(mode="json", exclude_none=True) if prm else None
        ),
        return_to=sanitize_return_to(return_to),
        web_origin=sanitize_web_origin(web_origin),
        browser_nonce=browser_nonce,
        client_secret=client_info.client_secret or "",
    )
    redis = _cache_client()
    stored = await redis.set(
        f"{_STATE_KEY_PREFIX}{state}",
        record.model_dump_json(),
        nx=True,
        ex=STATE_TTL_SECONDS,
    )
    if not stored:
        raise McpOAuthError("state collision — retry the connect")

    authorize_url = f"{authorize_endpoint}?{urlencode(params)}"
    logger.info(
        "[mcp_oauth] connect started user=%s server=%s issuer=%s",
        user_id, server_name, record.issuer,
    )
    return StartedConnect(
        authorize_url=authorize_url, state=state, browser_nonce=browser_nonce
    )


async def _claim_state(state: str) -> ConnectState | None:
    """Atomic single-use claim: at most one callback wins a given state."""
    redis = _cache_client()
    key = f"{_STATE_KEY_PREFIX}{state}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.get(key)
        pipe.delete(key)
        raw, _ = await pipe.execute()
    if not raw:
        return None
    try:
        return ConnectState.model_validate_json(raw)
    except ValidationError as e:
        # The key is already consumed, so an unusable record is spent — same
        # outcome as an expired one, and the caller must not leak the shape.
        logger.warning("[mcp_oauth] unusable state record discarded: %s", e)
        return None


async def complete_callback(
    *,
    state: str | None,
    code: str | None,
    iss: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    browser_nonce: str | None = None,
) -> str:
    """Phase 2: claim state, exchange the code, persist the bundle.

    Returns the redirect target for the browser — absolute when the start
    request captured a web origin, relative otherwise. Never raises for
    user-visible outcomes — errors are encoded in the redirect.

    ``browser_nonce`` is the value from the initiating browser's HttpOnly
    cookie; it must match the one minted into the state record, so a callback
    replayed in a different browser (which carries no such cookie) is refused.
    """
    if not state:
        return redirect_to(mcp_error="missing_state")
    record = await _claim_state(state)
    if record is None:
        # Unknown, expired, or already used — uniform answer, no oracle.
        return redirect_to(mcp_error="invalid_state")

    server_name = record.server_name

    def _fail(reason: str) -> str:
        logger.warning(
            "[mcp_oauth] callback failed user=%s server=%s reason=%s",
            record.user_id, server_name, reason,
        )
        # Absolute when the start request carried a browser Origin (split-port
        # dev: the callback's own origin is the API, which has no UI routes);
        # relative otherwise, resolving on the unified proxy/prod origin.
        return redirect_to(
            record.return_to, record.web_origin,
            mcp_error=reason, server=server_name,
        )

    # CSRF binding: the state is single-use and now claimed, so a mismatch here
    # spends it (no retry oracle). An older-shaped record has an empty nonce and
    # skips the check — it predates this control and can't be forged into one.
    if record.browser_nonce and not secrets.compare_digest(
        record.browser_nonce, browser_nonce or ""
    ):
        return _fail("state_mismatch")

    if error:
        # The AS reported denial/failure (user hit cancel, etc.).
        logger.info(
            "[mcp_oauth] authorization denied server=%s error=%s (%s)",
            server_name, error, error_description or "",
        )
        return _fail("denied" if error == "access_denied" else "provider_error")
    if not code:
        return _fail("missing_code")

    as_metadata = OAuthMetadata.model_validate(record.as_metadata)
    try:
        validate_authorization_response_iss(iss, as_metadata)
    except Exception:
        return _fail("issuer_mismatch")

    client_info = OAuthClientInformationFull.model_validate(record.client_info)
    # Re-attach the out-of-band secret (stripped from the blob at persist) so a
    # confidential client authenticates its token exchange below.
    if record.client_secret:
        client_info.client_secret = record.client_secret

    grant: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": record.redirect_uri,
        "client_id": client_info.client_id or "",
        "code_verifier": record.code_verifier,
    }
    if record.resource:
        grant["resource"] = record.resource

    try:
        token = await exchange_token(
            record.token_endpoint, grant, client_info=client_info
        )
    except TokenExchangeError as e:
        if e.kind is TokenFailure.BLOCKED:
            logger.warning("[mcp_oauth] token hop blocked: %s", e)
            return _fail("blocked_endpoint")
        logger.warning("[mcp_oauth] token exchange for %s: %s", server_name, e)
        return _fail("token_exchange_failed")
    except Exception:
        logger.exception("[mcp_oauth] token exchange errored for %s", server_name)
        return _fail("token_exchange_failed")

    connection_id = await upsert_connection(
        record.user_id,
        server_name,
        server_url=record.server_url,
        access_token=token.access_token,
        refresh_token=token.refresh_token,
        client_secret=client_info.client_secret,
        token_type=token.token_type,
        scope=token.scope or record.scope,
        expires_at=token.expires_at,
        client_info=record.client_info,
        as_metadata=record.as_metadata,
        resource_metadata=record.resource_metadata,
    )
    logger.info(
        "[mcp_oauth] connected user=%s server=%s connection=%s has_refresh=%s",
        record.user_id, server_name,
        connection_id, token.refresh_token is not None,
    )

    # Sessions must re-resolve: the server is now relay-bound.
    await bump_user_workspaces_mcp_version(record.user_id)

    # Best-effort host-side discovery so tools show up immediately; failure
    # leaves a pending/error schema row, never a broken connection.
    try:
        from src.server.services.mcp_oauth.discovery import (
            refresh_user_tool_schemas,
        )

        await refresh_user_tool_schemas(record.user_id, server_name)
    except Exception:
        logger.warning(
            "[mcp_oauth] post-connect discovery failed for %s",
            server_name, exc_info=True,
        )

    return redirect_to(
        record.return_to, record.web_origin, mcp_connected=server_name
    )
