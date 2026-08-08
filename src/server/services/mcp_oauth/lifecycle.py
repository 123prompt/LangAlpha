"""Token lifecycle: refresh single-flight, disconnect, status transitions.

The hot path takes NO lock while the access token has >10 minutes left. When
due, ``pg_try_advisory_lock`` (never blocking) elects one refresher across all
workers; losers use the still-valid old token immediately, or briefly poll the
row near expiry. The commit is a ``token_generation`` CAS so a stale winner
can never clobber a newer bundle.

An ambiguous refresh HTTP timeout is NOT retryable — the refresh token may
already be consumed server-side. The connection flips to ``refresh_ambiguous``
(old access token keeps serving until expiry, UI warns); a definitive
``invalid_grant`` flips to ``needs_reauth`` (blocks calls).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import anyio
import httpx2

from mcp.client.auth.utils import handle_token_response_scopes

from src.server.database.mcp_oauth import (
    ConnectionStatus,
    Secrets,
    commit_refresh,
    get_connection_by_id,
    mark_needs_reauth,
    mark_status,
)
from src.server.database.egress_grants import revoke_grants_for_connection
from src.server.services.writer_guard import advisory_key
from src.server.services.mcp_oauth.http import (
    OAuthHopBlocked,
    oauth_http_client,
    pinned_request,
)

logger = logging.getLogger(__name__)

# No lock while more than this much validity remains.
REFRESH_MARGIN_SECONDS = 600
# A loser may keep using the old token down to this floor.
OLD_TOKEN_FLOOR_SECONDS = 60
# Near-expiry losers poll the row for the winner's commit up to this long.
LOSER_POLL_SECONDS = 2.0
REFRESH_TIMEOUT = httpx2.Timeout(10.0, connect=5.0)


class TokenUnavailable(Exception):
    """No usable access token: carries a machine-readable reason."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(detail or reason)


@dataclass(frozen=True, slots=True)
class AccessToken:
    """A usable vendor bearer, tagged with the bundle generation it came from.

    ``generation`` is what makes a rotation observable: every write that
    replaces the access token increments it, so a holder can tell "the bundle
    moved under me" from "the vendor is rejecting the current token".
    """

    access_token: str
    token_type: str
    generation: int

    def header(self) -> str:
        return f"{self.token_type} {self.access_token}"


def _expiry_seconds(row: dict) -> float | None:
    expires_at = row.get("expires_at")
    if expires_at is None:
        return None  # non-expiring token
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return (expires_at - datetime.now(timezone.utc)).total_seconds()


def _usable(row: dict, *, floor: float = 0.0) -> bool:
    remaining = _expiry_seconds(row)
    return remaining is None or remaining > floor


async def ensure_fresh_access_token(connection_id: str) -> AccessToken:
    """Return a usable :class:`AccessToken` for a live connection.

    Raises :class:`TokenUnavailable` with reason ``needs_reauth`` /
    ``revoked`` / ``refresh_in_progress`` / ``expired``.
    """
    # Bearer-only: this runs on every relayed tool call, and the refresh token
    # and client secret are needed only if we actually end up refreshing —
    # which re-reads the full bundle under the lock anyway.
    row = await get_connection_by_id(connection_id, secrets=Secrets.BEARER)
    if row is None:
        raise TokenUnavailable("unknown_connection")
    status = row["status"]
    if status == ConnectionStatus.REVOKED:
        raise TokenUnavailable("revoked")
    if status == ConnectionStatus.NEEDS_REAUTH:
        raise TokenUnavailable("needs_reauth")

    remaining = _expiry_seconds(row)
    if remaining is None or remaining > REFRESH_MARGIN_SECONDS:
        return _token_view(row)
    if not row.get("has_refresh_token"):
        # No refresh token: ride the access token to expiry, then re-auth.
        if remaining > 0:
            return _token_view(row)
        await mark_status(connection_id, ConnectionStatus.NEEDS_REAUTH)
        raise TokenUnavailable("needs_reauth", "access token expired, no refresh token")
    if status == ConnectionStatus.REFRESH_AMBIGUOUS:
        # Never re-attempt an ambiguous refresh; serve until expiry.
        if remaining > 0:
            return _token_view(row)
        await mark_status(connection_id, ConnectionStatus.NEEDS_REAUTH)
        raise TokenUnavailable("needs_reauth", "ambiguous refresh, token expired")

    return await _refresh_single_flight(connection_id, row)


async def current_access_token(connection_id: str) -> AccessToken | None:
    """The stored bearer as-is — no refresh, no status gate.

    For a caller that already sent a token and got a 401: the question is
    whether the stored bundle has since moved, which is about the row, not
    about freshness.
    """
    row = await get_connection_by_id(connection_id, secrets=Secrets.BEARER)
    if row is None or not row.get("access_token"):
        return None
    return _token_view(row)


async def mark_connection_needs_reauth(
    connection_id: str, *, seen_token_generation: int
) -> bool:
    """Record that the vendor rejected the bundle at ``seen_token_generation``.

    A no-op unless that generation is still the current one and the connection
    is still ``connected`` — a 401 against a bundle that has already been
    replaced is stale news, and the terminal states are not ours to overwrite.
    """
    flipped = await mark_needs_reauth(
        connection_id, expected_generation=seen_token_generation
    )
    if flipped:
        logger.warning(
            "[mcp_oauth] vendor rejected a current token; connection %s "
            "flipped to needs_reauth",
            connection_id,
        )
    return flipped


def _token_view(row: dict) -> AccessToken:
    return AccessToken(
        access_token=row["access_token"],
        token_type=row.get("token_type") or "Bearer",
        generation=row["token_generation"],
    )


async def _refresh_single_flight(connection_id: str, row: dict) -> AccessToken:
    """Try-lock refresh: one winner per cluster; losers never block on it."""
    from src.server.database.pool import get_db_connection

    key = advisory_key("mcp_oauth_refresh", connection_id)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            won = (await cur.fetchone())[0]
        if not won:
            return await _wait_for_winner(connection_id, row)
        try:
            # Re-read under the lock on the HELD connection — the previous
            # winner may already have committed a fresh bundle. Full bundle:
            # this is the one path that spends the refresh token and client
            # secret. Reusing `conn` keeps the whole refresh on one pool slot
            # instead of nesting a second acquire while this one is held.
            current = await get_connection_by_id(
                connection_id, secrets=Secrets.FULL, conn=conn
            )
            if current is None:
                raise TokenUnavailable("unknown_connection")
            fresh_remaining = _expiry_seconds(current)
            if fresh_remaining is None or fresh_remaining > REFRESH_MARGIN_SECONDS:
                return _token_view(current)
            return await _do_refresh(connection_id, current, conn=conn)
        finally:
            # The advisory lock is session-scoped to THIS connection and the
            # pool does not reset it on return (no DISCARD configured), so an
            # unshielded unlock skipped by a re-delivered CancelledError would
            # strand the cluster-wide election on this pooled connection —
            # every later refresher then loses the try-lock until the
            # connection is recycled. Shield so the unlock always runs.
            with anyio.CancelScope(shield=True):
                async with conn.cursor() as cur:
                    await cur.execute("SELECT pg_advisory_unlock(%s)", (key,))


async def _wait_for_winner(connection_id: str, row: dict) -> AccessToken:
    """Loser path: old token if comfortably valid, else briefly poll the row."""
    if _usable(row, floor=OLD_TOKEN_FLOOR_SECONDS):
        return _token_view(row)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LOSER_POLL_SECONDS
    generation = row["token_generation"]
    while loop.time() < deadline:
        await asyncio.sleep(0.25)
        current = await get_connection_by_id(connection_id, secrets=Secrets.BEARER)
        if current is None:
            raise TokenUnavailable("unknown_connection")
        if current["token_generation"] > generation and _usable(current):
            return _token_view(current)
    if _usable(row):
        return _token_view(row)
    raise TokenUnavailable("refresh_in_progress")


async def _do_refresh(connection_id: str, row: dict, *, conn=None) -> AccessToken:
    """Winner path: one refresh POST, generation-CAS commit.

    ``conn`` is the pool connection already holding this refresh's advisory
    lock; every DB write here runs on it so the refresh never occupies a second
    pool slot.
    """
    data: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": row["refresh_token"],
        "client_id": (row.get("client_info") or {}).get("client_id") or "",
    }
    if row.get("resource_metadata"):
        resource = (row.get("resource_metadata") or {}).get("resource")
        if resource:
            data["resource"] = str(resource)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    client_secret = row.get("client_secret")
    if client_secret:
        # Apply the client's registered token-endpoint auth method, mirroring
        # the SDK's prepare_token_auth used on the connect path (RFC 6749
        # §2.3.1): client_secret_basic carries credentials in the Authorization
        # header; client_secret_post (and the historical absent default) carry
        # them in the body. Hardcoding the body form 401s a basic-auth client.
        client_info = row.get("client_info") or {}
        if client_info.get("token_endpoint_auth_method") == "client_secret_basic":
            import base64
            from urllib.parse import quote

            client_id = client_info.get("client_id") or ""
            creds = f"{quote(client_id, safe='')}:{quote(client_secret, safe='')}"
            headers["Authorization"] = (
                "Basic " + base64.b64encode(creds.encode()).decode()
            )
        else:
            data["client_secret"] = client_secret

    token_endpoint = (row.get("as_metadata") or {}).get("token_endpoint")
    if not token_endpoint:
        await mark_status(connection_id, ConnectionStatus.NEEDS_REAUTH, conn=conn)
        raise TokenUnavailable("needs_reauth", "no token endpoint on record")

    try:
        async with oauth_http_client() as client:
            client.timeout = REFRESH_TIMEOUT
            response = await pinned_request(
                client, "POST", str(token_endpoint), headers=headers, data=data
            )
    except (httpx2.TimeoutException, OAuthHopBlocked) as e:
        # Ambiguous: the AS may have rotated the refresh token already.
        # Retrying could burn the one-time token — flip to ambiguous and
        # keep serving the old access token until it expires.
        logger.warning(
            "[mcp_oauth] ambiguous refresh for %s: %s", connection_id, e
        )
        await mark_status(connection_id, ConnectionStatus.REFRESH_AMBIGUOUS, conn=conn)
        if _usable(row):
            return _token_view(row)
        raise TokenUnavailable("needs_reauth", "ambiguous refresh, token expired")
    except Exception as e:
        # Transport-level failure before the request could have been consumed.
        logger.warning("[mcp_oauth] refresh transport error for %s: %s", connection_id, e)
        if _usable(row):
            return _token_view(row)
        raise TokenUnavailable("expired", "refresh unreachable, token expired")

    if response.status_code != 200:
        # Log the status only — never the vendor's response body. A failed
        # token exchange carries no new token, and an AS is free to echo the
        # request (client_id, even the refresh token) back in an error body;
        # keeping it out of our logs removes that whole class of leak.
        if response.status_code in (400, 401):
            logger.warning(
                "[mcp_oauth] refresh rejected for %s (status %s)",
                connection_id, response.status_code,
            )
            await mark_status(connection_id, ConnectionStatus.NEEDS_REAUTH, conn=conn)
            raise TokenUnavailable("needs_reauth", "refresh token rejected")
        # 5xx: transient server-side trouble — keep status, ride the old token.
        logger.warning(
            "[mcp_oauth] refresh failed for %s (status %s)",
            connection_id, response.status_code,
        )
        if _usable(row):
            return _token_view(row)
        raise TokenUnavailable("expired", "refresh failing, token expired")

    token = await handle_token_response_scopes(response)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=token.expires_in)
        if token.expires_in
        else None
    )
    committed = await commit_refresh(
        connection_id,
        expected_generation=row["token_generation"],
        access_token=token.access_token,
        # None keeps the stored one; "" would otherwise overwrite a working
        # refresh token with an encrypted empty string.
        refresh_token=token.refresh_token or None,
        expires_at=expires_at,
        scope=token.scope,
        conn=conn,
    )
    if not committed:
        # Lost the CAS (should not happen under the lock; defensive): serve
        # whatever is now current.
        current = await get_connection_by_id(
            connection_id, secrets=Secrets.BEARER, conn=conn
        )
        if current and _usable(current):
            return _token_view(current)
        raise TokenUnavailable("refresh_in_progress")
    logger.info(
        "[mcp_oauth] refreshed connection %s (rotated_refresh=%s)",
        connection_id, token.refresh_token is not None,
    )
    # The CAS above committed exactly one increment over the generation we read.
    return AccessToken(
        access_token=token.access_token,
        token_type=token.token_type or "Bearer",
        generation=row["token_generation"] + 1,
    )


async def disconnect_server(user_id: str, server_name: str) -> bool:
    """Disconnect: revoke the connection + its grants, drop schemas, fan out.

    Revocation is instant — the relay checks grant/connection status per
    request, so no sandbox convergence is needed. Vendor-side revocation lives
    in the vendor's own connected-apps page; we only drop our copy.
    """
    from src.server.database.mcp_oauth import get_connection
    from src.server.database.mcp_servers import (
        bump_user_workspaces_mcp_version,
        delete_user_tool_schemas,
    )

    row = await get_connection(user_id, server_name)
    if row is None:
        return False
    await mark_status(row["connection_id"], ConnectionStatus.REVOKED)
    await revoke_grants_for_connection(row["connection_id"])
    await delete_user_tool_schemas(user_id, server_name)
    await bump_user_workspaces_mcp_version(user_id)
    logger.info(
        "[mcp_oauth] disconnected user=%s server=%s connection=%s",
        user_id, server_name, row["connection_id"],
    )
    return True
