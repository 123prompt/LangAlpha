"""SSRF-pinned HTTP for OAuth hops (httpx2 — the SDK's request objects).

Every host-side request to a user-supplied URL (server probe, PRM, AS
metadata, DCR, token) goes through :func:`pinned_request`: the hostname is
resolved once, every address is required to be globally routable, and the
request is sent to the validated IP with the Host header + SNI restored — so a
DNS answer swapped between validation and connect cannot re-target the
request. Redirects are refused outright (fail closed).

The SDK builds ``httpx2`` requests; this module owns the matching client.
"""

from __future__ import annotations

import httpx2

from src.server.utils.egress_guard import EgressBlockedError, pin_public_url

# One ladder for every OAuth hop: these are interactive, user-facing calls.
DEFAULT_TIMEOUT = httpx2.Timeout(15.0, connect=5.0)

USER_AGENT = "langalpha-mcp-connect/1"


class OAuthHopBlocked(Exception):
    """A hop failed SSRF validation or tried to redirect."""


def oauth_http_client() -> httpx2.AsyncClient:
    """Client for OAuth hops: no redirects, no env proxies, short timeouts."""
    return httpx2.AsyncClient(
        follow_redirects=False,
        trust_env=False,
        timeout=DEFAULT_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
    )


async def pinned_request(
    client: httpx2.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    content: bytes | None = None,
) -> httpx2.Response:
    """Send one SSRF-pinned request; refuse redirects."""
    try:
        target = await pin_public_url(url, require_https=True)
    except EgressBlockedError as e:
        raise OAuthHopBlocked(str(e)) from e
    url_pinned, send_headers, extensions = target.pinned_kwargs(headers)
    response = await client.request(
        method,
        url_pinned,
        headers=send_headers,
        data=data,
        content=content,
        extensions=extensions,
    )
    if response.is_redirect:
        raise OAuthHopBlocked(
            f"{method} {url} answered a redirect ({response.status_code}); "
            "redirects are refused on OAuth hops"
        )
    return response


async def pinned_send(
    client: httpx2.AsyncClient, request: httpx2.Request
) -> httpx2.Response:
    """Re-issue an SDK-built request through the pinned path."""
    body = request.read()
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }
    return await pinned_request(
        client,
        request.method,
        str(request.url),
        headers=headers,
        content=body or None,
    )
