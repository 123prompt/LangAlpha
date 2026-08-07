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

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

from mcp.client.auth import PKCEParameters
from mcp.client.auth.oauth2 import OAuthContext
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
    handle_token_response_scopes,
    validate_authorization_response_iss,
    validate_metadata_issuer,
)
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    ProtectedResourceMetadata,
)

from src.config.env import SERVER_BASE_URL
from src.server.database.mcp_oauth import get_connection, upsert_connection
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

logger = logging.getLogger(__name__)

STATE_TTL_SECONDS = 600
_STATE_KEY_PREFIX = "mcp:oauth:state:"

CLIENT_NAME = "Langalpha"
DEFAULT_RETURN_TO = "/connectors"

# The MCP endpoint probe advertises a protocol version so servers answer with
# era-appropriate WWW-Authenticate hints.
_PROBE_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-06-18",
}


class McpOAuthError(Exception):
    """A connect-flow step failed in a way the caller should surface."""


def _redirect_uri() -> str:
    return f"{SERVER_BASE_URL.rstrip('/')}/api/v1/mcp/oauth/callback"


def sanitize_return_to(value: str | None) -> str:
    """Allowlist: a same-app relative path only ('/x', never '//x' or a URL)."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return DEFAULT_RETURN_TO


def _cache_client():
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not (cache.enabled and cache.client):
        raise McpOAuthError("Redis is required for the OAuth connect flow")
    return cache.client


def _build_context(
    server_url: str,
    *,
    client_metadata: OAuthClientMetadata,
    prm: ProtectedResourceMetadata | None,
    as_metadata: OAuthMetadata | None,
    auth_server_url: str | None,
    client_info: OAuthClientInformationFull | None = None,
) -> OAuthContext:
    """Reconstruct the SDK's flow context from persisted pieces.

    Storage/redirect/callback are the in-process provider's affordances — the
    two-phase flow never uses them, so they are None. The context is used only
    for its pure helpers (resource URL, token auth preparation).
    """
    return OAuthContext(
        server_url=server_url,
        client_metadata=client_metadata,
        storage=None,  # type: ignore[arg-type]
        redirect_handler=None,
        callback_handler=None,
        protected_resource_metadata=prm,
        oauth_metadata=as_metadata,
        auth_server_url=auth_server_url,
        protocol_version=_PROBE_HEADERS["MCP-Protocol-Version"],
        client_info=client_info,
    )


async def _discover(client, server_url: str) -> tuple[
    ProtectedResourceMetadata | None, OAuthMetadata, str | None, str | None
]:
    """Run 401-probe → PRM → AS-metadata discovery. Returns
    (prm, as_metadata, auth_server_url, www_scope)."""
    www_auth_url: str | None = None
    www_scope: str | None = None
    try:
        probe = await pinned_request(
            client, "GET", server_url, headers=_PROBE_HEADERS
        )
        if probe.status_code == 401:
            www_auth_url = extract_resource_metadata_from_www_auth(probe)
            www_scope = extract_scope_from_www_auth(probe)
    except OAuthHopBlocked:
        raise
    except Exception as e:
        # The probe is a hint source only — discovery can proceed without it.
        logger.info("[mcp_oauth] probe of %s failed: %s", server_url, e)

    prm: ProtectedResourceMetadata | None = None
    for url in build_protected_resource_metadata_discovery_urls(
        www_auth_url, server_url
    ):
        try:
            resp = await pinned_request(client, "GET", url, headers=_PROBE_HEADERS)
        except OAuthHopBlocked:
            raise
        except Exception:
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
        try:
            resp = await pinned_request(client, "GET", url, headers=_PROBE_HEADERS)
        except OAuthHopBlocked:
            raise
        except Exception:
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
    existing = await get_connection(user_id, server_name, decrypt=True)
    if existing and existing.get("client_info"):
        try:
            stored = OAuthClientInformationFull.model_validate(
                existing["client_info"]
            )
            stored_issuer = (existing.get("as_metadata") or {}).get("issuer")
            if stored.client_id and stored_issuer == str(as_metadata.issuer):
                # client_secret is stored encrypted, outside the JSONB blob.
                stored.client_secret = existing.get("client_secret")
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
    user_id: str, server_name: str, *, return_to: str | None = None
) -> dict:
    """Phase 1: discovery + DCR + state/PKCE persist. Returns {authorize_url}."""
    row = await get_catalog_server(user_id, server_name)
    if row is None:
        raise McpOAuthError("MCP server not found")
    server_url = row.get("url")
    if row.get("transport") not in ("http", "sse") or not server_url:
        raise McpOAuthError("OAuth connect requires a remote (http) MCP server")

    redirect_uri = _redirect_uri()
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
        context = _build_context(
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

    record = {
        "user_id": user_id,
        "server_name": server_name,
        "server_url": server_url,
        "code_verifier": pkce.code_verifier,
        "redirect_uri": redirect_uri,
        "token_endpoint": str(as_metadata.token_endpoint),
        "issuer": str(as_metadata.issuer),
        "resource": params.get("resource"),
        "scope": effective_scope,
        "client_info": client_info.model_dump(mode="json", exclude_none=True),
        "as_metadata": as_metadata.model_dump(mode="json", exclude_none=True),
        "resource_metadata": (
            prm.model_dump(mode="json", exclude_none=True) if prm else None
        ),
        "return_to": sanitize_return_to(return_to),
    }
    redis = _cache_client()
    stored = await redis.set(
        f"{_STATE_KEY_PREFIX}{state}",
        json.dumps(record),
        nx=True,
        ex=STATE_TTL_SECONDS,
    )
    if not stored:
        raise McpOAuthError("state collision — retry the connect")

    authorize_url = f"{authorize_endpoint}?{urlencode(params)}"
    logger.info(
        "[mcp_oauth] connect started user=%s server=%s issuer=%s",
        user_id, server_name, record["issuer"],
    )
    return {"authorize_url": authorize_url, "state": state}


async def _claim_state(state: str) -> dict | None:
    """Atomic single-use claim: at most one callback wins a given state."""
    redis = _cache_client()
    key = f"{_STATE_KEY_PREFIX}{state}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.get(key)
        pipe.delete(key)
        raw, _ = await pipe.execute()
    if not raw:
        return None
    return json.loads(raw)


async def complete_callback(
    *,
    state: str | None,
    code: str | None,
    iss: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> str:
    """Phase 2: claim state, exchange the code, persist the bundle.

    Returns the redirect target (relative path) for the browser. Never raises
    for user-visible outcomes — errors are encoded in the redirect.
    """
    if not state:
        return f"{DEFAULT_RETURN_TO}?mcp_error=missing_state"
    record = await _claim_state(state)
    if record is None:
        # Unknown, expired, or already used — uniform answer, no oracle.
        return f"{DEFAULT_RETURN_TO}?mcp_error=invalid_state"

    return_to = sanitize_return_to(record.get("return_to"))
    server_name = record["server_name"]

    def _fail(reason: str) -> str:
        logger.warning(
            "[mcp_oauth] callback failed user=%s server=%s reason=%s",
            record["user_id"], server_name, reason,
        )
        return f"{return_to}?mcp_error={reason}&server={quote(server_name)}"

    if error:
        # The AS reported denial/failure (user hit cancel, etc.).
        logger.info(
            "[mcp_oauth] authorization denied server=%s error=%s (%s)",
            server_name, error, error_description or "",
        )
        return _fail("denied" if error == "access_denied" else "provider_error")
    if not code:
        return _fail("missing_code")

    as_metadata = OAuthMetadata.model_validate(record["as_metadata"])
    try:
        validate_authorization_response_iss(iss, as_metadata)
    except Exception:
        return _fail("issuer_mismatch")

    client_info = OAuthClientInformationFull.model_validate(record["client_info"])
    prm = (
        ProtectedResourceMetadata.model_validate(record["resource_metadata"])
        if record.get("resource_metadata")
        else None
    )
    context = _build_context(
        record["server_url"],
        client_metadata=OAuthClientMetadata(
            client_name=CLIENT_NAME,
            redirect_uris=[record["redirect_uri"]],  # type: ignore[list-item]
        ),
        prm=prm,
        as_metadata=as_metadata,
        auth_server_url=None,
        client_info=client_info,
    )

    token_data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": record["redirect_uri"],
        "client_id": client_info.client_id or "",
        "code_verifier": record["code_verifier"],
    }
    if record.get("resource"):
        token_data["resource"] = record["resource"]
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    token_data, headers = context.prepare_token_auth(token_data, headers)

    try:
        async with oauth_http_client() as client:
            response = await pinned_request(
                client,
                "POST",
                record["token_endpoint"],
                headers=headers,
                data=token_data,
            )
        if response.status_code != 200:
            body = (await response.aread())[:300]
            logger.warning(
                "[mcp_oauth] token exchange %s for %s: %s",
                response.status_code, server_name, body,
            )
            return _fail("token_exchange_failed")
        token = await handle_token_response_scopes(response)
    except OAuthHopBlocked as e:
        logger.warning("[mcp_oauth] token hop blocked: %s", e)
        return _fail("blocked_endpoint")
    except Exception:
        logger.exception("[mcp_oauth] token exchange errored for %s", server_name)
        return _fail("token_exchange_failed")

    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=token.expires_in)
        if token.expires_in
        else None
    )
    connection_id = await upsert_connection(
        record["user_id"],
        server_name,
        server_url=record["server_url"],
        access_token=token.access_token,
        refresh_token=token.refresh_token,
        client_secret=client_info.client_secret,
        token_type=token.token_type or "Bearer",
        scope=token.scope or record.get("scope"),
        expires_at=expires_at,
        client_info=record["client_info"],
        as_metadata=record["as_metadata"],
        resource_metadata=record.get("resource_metadata"),
    )
    logger.info(
        "[mcp_oauth] connected user=%s server=%s connection=%s has_refresh=%s",
        record["user_id"], server_name,
        connection_id, token.refresh_token is not None,
    )

    # Sessions must re-resolve: the server is now relay-bound.
    await bump_user_workspaces_mcp_version(record["user_id"])

    # Best-effort host-side discovery so tools show up immediately; failure
    # leaves a pending/error schema row, never a broken connection.
    try:
        from src.server.services.mcp_oauth.discovery import (
            refresh_user_tool_schemas,
        )

        await refresh_user_tool_schemas(record["user_id"], server_name)
    except Exception:
        logger.warning(
            "[mcp_oauth] post-connect discovery failed for %s",
            server_name, exc_info=True,
        )

    return f"{return_to}?mcp_connected={quote(server_name)}"
