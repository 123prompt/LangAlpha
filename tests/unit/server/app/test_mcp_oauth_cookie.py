"""The OAuth CSRF nonce cookie is per-state, so concurrent connects from one
browser don't clobber each other's cookie.

The bug this pins: a single fixed cookie name meant a second connect overwrote
the first flow's nonce cookie (same name, same path), so the first flow's
callback read the second flow's nonce and failed ``state_mismatch``. Naming the
cookie for the single-use ``state`` isolates the flows.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import Response

from src.server.app import mcp_oauth as mod
from src.server.services.mcp_oauth import StartedConnect


def _request(*, origin: str | None = None, cookies: dict | None = None):
    return SimpleNamespace(
        headers={"origin": origin} if origin else {},
        cookies=cookies or {},
    )


def _set_cookies(response: Response) -> list[str]:
    return [
        v.decode() for k, v in response.raw_headers if k == b"set-cookie"
    ]


@pytest.mark.asyncio
async def test_start_names_the_cookie_for_its_state(monkeypatch):
    async def _start(user_id, name, *, return_to, web_origin):
        return StartedConnect(
            authorize_url="https://as.test/authorize?state=state-A",
            state="state-A",
            browser_nonce="nonce-A",
        )

    monkeypatch.setattr(mod, "start_connect", _start)
    response = Response()

    await mod.oauth_start("srv", "user-1", _request(), response, None)

    [cookie] = _set_cookies(response)
    assert cookie.startswith("mcp_oauth_cb_state-A=nonce-A")
    assert "path=/api/v1/mcp/oauth" in cookie.lower()
    assert "httponly" in cookie.lower()


@pytest.mark.asyncio
async def test_two_concurrent_starts_do_not_share_a_cookie_name(monkeypatch):
    flows = iter(
        [
            StartedConnect(authorize_url="u", state="state-A", browser_nonce="nonce-A"),
            StartedConnect(authorize_url="u", state="state-B", browser_nonce="nonce-B"),
        ]
    )

    async def _start(user_id, name, *, return_to, web_origin):
        return next(flows)

    monkeypatch.setattr(mod, "start_connect", _start)

    r1, r2 = Response(), Response()
    await mod.oauth_start("srv", "user-1", _request(), r1, None)
    await mod.oauth_start("srv", "user-1", _request(), r2, None)

    [c1] = _set_cookies(r1)
    [c2] = _set_cookies(r2)
    # Distinct names → the second start cannot overwrite the first's nonce.
    assert c1.startswith("mcp_oauth_cb_state-A=nonce-A")
    assert c2.startswith("mcp_oauth_cb_state-B=nonce-B")


@pytest.mark.asyncio
async def test_loopback_start_sets_no_cookie(monkeypatch):
    async def _start(user_id, name, *, return_to, web_origin):
        # Loopback callback → no nonce minted (see redirects.callback_is_loopback).
        return StartedConnect(authorize_url="u", state="state-A", browser_nonce="")

    monkeypatch.setattr(mod, "start_connect", _start)
    response = Response()

    await mod.oauth_start("srv", "user-1", _request(), response, None)

    assert _set_cookies(response) == []


@pytest.mark.asyncio
async def test_callback_reads_the_cookie_named_for_its_state(monkeypatch):
    seen = {}

    async def _complete(*, state, code, iss, error, error_description, browser_nonce):
        seen["state"] = state
        seen["browser_nonce"] = browser_nonce
        return "/connectors?mcp_connected=srv"

    monkeypatch.setattr(mod, "complete_callback", _complete)
    # The browser carries BOTH flows' cookies; the callback must pick its own.
    request = _request(
        cookies={
            "mcp_oauth_cb_state-A": "nonce-A",
            "mcp_oauth_cb_state-B": "nonce-B",
        }
    )

    resp = await mod.oauth_callback(request, state="state-A", code="code-1")

    assert seen["browser_nonce"] == "nonce-A"  # not nonce-B
    # And the response clears exactly this flow's cookie.
    [cleared] = _set_cookies(resp)
    assert cleared.startswith("mcp_oauth_cb_state-A=")
    assert 'max-age=0' in cleared.lower() or 'expires=' in cleared.lower()


@pytest.mark.asyncio
async def test_callback_without_state_reads_no_cookie(monkeypatch):
    seen = {}

    async def _complete(*, state, code, iss, error, error_description, browser_nonce):
        seen["browser_nonce"] = browser_nonce
        return "/connectors?mcp_error=missing_state"

    monkeypatch.setattr(mod, "complete_callback", _complete)
    request = _request(cookies={"mcp_oauth_cb_state-A": "nonce-A"})

    await mod.oauth_callback(request, state=None, code="code-1")

    assert seen["browser_nonce"] is None
