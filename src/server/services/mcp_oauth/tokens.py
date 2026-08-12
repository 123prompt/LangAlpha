"""The one token-endpoint request — connect's code exchange and the refresh.

Both grants are the same POST: form body, RFC 6749 §2.3.1 client auth from the
SDK's ``prepare_token_auth``, SSRF-pinned send, expiry converted to an absolute
instant. They differ only in the grant they carry and in how the caller reads
:class:`TokenExchangeError`.

The vendor's response body never leaves this module. A failed token request
carries no token, and an authorization server is free to echo the request
(client_id, even the refresh token) back inside an error body — so the body is
neither logged nor surfaced, and callers only ever see a status-derived detail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from typing import Any

import httpx2

from mcp.client.auth.oauth2 import OAuthContext
from mcp.client.auth.utils import handle_token_response_scopes
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    ProtectedResourceMetadata,
)

from src.server.services.mcp_oauth.http import (
    OAuthHopBlocked,
    oauth_http_client,
    pinned_request,
)

# Advertised on every hop so servers answer with era-appropriate hints.
PROTOCOL_VERSION = "2025-06-18"


class TokenFailure(Enum):
    """Why a token request produced no bundle.

    What separates them is whether the server may already have consumed the
    grant: ``BLOCKED``/``TIMEOUT`` are ambiguous, ``REJECTED`` is definitive,
    ``RETRYABLE`` (transport error, or a 5xx) leaves it unspent.
    """

    BLOCKED = auto()
    TIMEOUT = auto()
    REJECTED = auto()
    RETRYABLE = auto()


class TokenExchangeError(Exception):
    def __init__(self, kind: TokenFailure, detail: str = ""):
        self.kind = kind
        super().__init__(detail or kind.name.lower())


@dataclass(frozen=True, slots=True)
class ExchangedToken:
    """A token-endpoint bundle, normalized for storage."""

    access_token: str
    token_type: str
    scope: str | None
    refresh_token: str | None
    expires_at: datetime | None


def build_context(
    server_url: str = "",
    *,
    client_metadata: OAuthClientMetadata,
    prm: ProtectedResourceMetadata | None = None,
    as_metadata: OAuthMetadata | None = None,
    auth_server_url: str | None = None,
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
        protocol_version=PROTOCOL_VERSION,
        client_info=client_info,
    )


def registered_client(
    client_info: dict[str, Any], client_secret: str | None
) -> OAuthClientInformationFull:
    """The stored DCR blob as the SDK model, for its auth helper only.

    ``model_construct`` skips validation deliberately: the blob is whatever the
    authorization server returned at registration, and a refresh must not fail
    because an unrelated field of an old registration no longer validates. Only
    the three fields ``prepare_token_auth`` reads are supplied.
    """
    return OAuthClientInformationFull.model_construct(
        client_id=client_info.get("client_id") or "",
        client_secret=client_secret or None,
        token_endpoint_auth_method=client_info.get("token_endpoint_auth_method"),
    )


async def exchange_token(
    token_endpoint: str,
    grant: dict[str, str],
    *,
    client_info: OAuthClientInformationFull,
    timeout: httpx2.Timeout | None = None,
) -> ExchangedToken:
    """POST one grant to the token endpoint. Raises :class:`TokenExchangeError`.

    A malformed 200 body propagates as-is: it is a parse failure, not a
    classified refusal, and each caller already has a catch-all for it.
    """
    data, headers = build_context(
        client_metadata=client_info, client_info=client_info
    ).prepare_token_auth(
        dict(grant), {"Content-Type": "application/x-www-form-urlencoded"}
    )
    if client_info.client_secret and client_info.token_endpoint_auth_method is None:
        # A registration that never stated a method. The SDK sends no
        # credential at all in that case, which 401s a confidential client;
        # RFC 6749 §2.3.1 leaves the default to the server and the body form is
        # what such clients accept, so it stays the fallback here.
        data["client_secret"] = client_info.client_secret

    try:
        async with oauth_http_client() as client:
            if timeout is not None:
                client.timeout = timeout
            response = await pinned_request(
                client, "POST", token_endpoint, headers=headers, data=data
            )
    except OAuthHopBlocked as e:
        raise TokenExchangeError(TokenFailure.BLOCKED, str(e)) from e
    except httpx2.TimeoutException as e:
        raise TokenExchangeError(TokenFailure.TIMEOUT, f"timeout: {e}") from e
    except Exception as e:
        raise TokenExchangeError(TokenFailure.RETRYABLE, str(e)) from e

    if response.status_code != 200:
        raise TokenExchangeError(
            TokenFailure.REJECTED
            if response.status_code in (400, 401)
            else TokenFailure.RETRYABLE,
            f"status {response.status_code}",
        )

    token = await handle_token_response_scopes(response)
    return ExchangedToken(
        access_token=token.access_token,
        token_type=token.token_type or "Bearer",
        scope=token.scope,
        # "" and None both mean "no refresh token"; collapse them here so the
        # column's NOT NULL-ness is the single answer to "can this refresh?".
        refresh_token=token.refresh_token or None,
        expires_at=(
            datetime.now(timezone.utc) + timedelta(seconds=token.expires_in)
            if token.expires_in
            else None
        ),
    )
