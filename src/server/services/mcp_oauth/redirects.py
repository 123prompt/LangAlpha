"""Where the OAuth callback may send the browser, and how that URL is built.

Every user-visible outcome of the callback is a redirect, so the allowlisting
lives in one place: a same-app relative path, optionally prefixed by an origin
this deployment has vouched for. :func:`redirect_to` re-sanitizes both halves
on the way out, which is what makes it impossible for any path through the
callback to emit an unvetted redirect.
"""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from src.config.env import SERVER_BASE_URL

DEFAULT_RETURN_TO = "/connectors"


def callback_uri() -> str:
    return f"{SERVER_BASE_URL.rstrip('/')}/api/v1/mcp/oauth/callback"


def _host_is_loopback(host: str | None) -> bool:
    host = (host or "").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _server_origin() -> str:
    parts = urlsplit(SERVER_BASE_URL)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def callback_is_loopback() -> bool:
    """Whether the browser-nonce binding can be honored at all here.

    The cookie only returns if the origin the user browses shares a *host* with
    this callback (cookies ignore port, not host). A loopback callback means a
    local dev box, where the UI is routinely served from some other host — a
    ``*.localhost`` dev proxy — while an AS will only accept a loopback literal
    for an ``http`` redirect_uri. Those two demands can't both be met, so
    requiring the cookie there rejects every connect. A deployed instance never
    has a loopback callback, so the binding stays mandatory in production.
    """
    return _host_is_loopback(urlsplit(SERVER_BASE_URL).hostname or "")


def sanitize_return_to(value: str | None) -> str:
    """Allowlist: a same-app relative path only ('/x', never '//x' or '/\\x').

    A leading '/' followed by '/' or '\\' is protocol-relative once the browser
    normalizes backslashes ('/\\evil.com' → '//evil.com'), which would redirect
    off-site — so the second character must be a normal path char.
    """
    if value and value.startswith("/") and value[1:2] not in ("/", "\\"):
        return value
    return DEFAULT_RETURN_TO


def sanitize_web_origin(value: str | None) -> str:
    """A vouched-for http(s) origin (scheme://host[:port]) or "".

    Captured from the browser's Origin header on the authenticated start
    request — it is where the UI actually lives, which the callback's own
    origin is not when the frontend and API run on split dev ports. It becomes
    the prefix of the post-connect redirect, so it must never be an
    attacker-forged Origin: only a local dev host (the one case where the
    callback's origin legitimately differs from the UI's) or this deployment's
    own base URL is honored. Anything else — including a well-formed but foreign
    public origin — is dropped, and the callback falls back to a relative path
    on its own origin. Anything beyond a bare origin (path, query, userinfo,
    "null") is dropped up front.
    """
    if not value:
        return ""
    parts = urlsplit(value)
    if not (
        parts.scheme in ("http", "https")
        and parts.netloc
        and "@" not in parts.netloc
        and parts.path in ("", "/")
        and not parts.query
        and not parts.fragment
    ):
        return ""
    origin = f"{parts.scheme}://{parts.netloc}"
    if _host_is_loopback(parts.hostname) or origin == _server_origin():
        return origin
    return ""


def redirect_to(
    return_to: str | None = None, web_origin: str | None = None, /, **params: str
) -> str:
    """The callback's answer: an allowlisted target carrying outcome params.

    ``params`` merge into whatever query the return path already carries, so a
    ``return_to`` of ``/connectors?tab=oauth`` gains ``&mcp_error=…`` instead of
    a second ``?``. Encoding is ``%20``-style to match what the UI parses.
    """
    target = sanitize_web_origin(web_origin) + sanitize_return_to(return_to)
    parts = urlsplit(target)
    query = urlencode(
        parse_qsl(parts.query) + list(params.items()), quote_via=quote
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
