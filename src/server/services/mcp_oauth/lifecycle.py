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
from datetime import datetime, timedelta, timezone

import httpx2

from mcp.client.auth.utils import handle_token_response_scopes

from src.server.database.mcp_oauth import (
    commit_refresh,
    get_connection_by_id,
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


async def ensure_fresh_access_token(connection_id: str) -> dict:
    """Return ``{access_token, token_type, server_name}`` for a live connection.

    Raises :class:`TokenUnavailable` with reason ``needs_reauth`` /
    ``revoked`` / ``refresh_in_progress`` / ``expired``.
    """
    row = await get_connection_by_id(connection_id, decrypt=True)
    if row is None:
        raise TokenUnavailable("unknown_connection")
    status = row["status"]
    if status == "revoked":
        raise TokenUnavailable("revoked")
    if status == "needs_reauth":
        raise TokenUnavailable("needs_reauth")

    remaining = _expiry_seconds(row)
    if remaining is None or remaining > REFRESH_MARGIN_SECONDS:
        return _token_view(row)
    if not row.get("refresh_token"):
        # No refresh token: ride the access token to expiry, then re-auth.
        if remaining > 0:
            return _token_view(row)
        await mark_status(connection_id, "needs_reauth")
        raise TokenUnavailable("needs_reauth", "access token expired, no refresh token")
    if status == "refresh_ambiguous":
        # Never re-attempt an ambiguous refresh; serve until expiry.
        if remaining > 0:
            return _token_view(row)
        await mark_status(connection_id, "needs_reauth")
        raise TokenUnavailable("needs_reauth", "ambiguous refresh, token expired")

    return await _refresh_single_flight(connection_id, row)


def _token_view(row: dict) -> dict:
    return {
        "access_token": row["access_token"],
        "token_type": row.get("token_type") or "Bearer",
        "server_name": row["server_name"],
        "status": row["status"],
    }


async def _refresh_single_flight(connection_id: str, row: dict) -> dict:
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
                # Re-read under the lock — the previous winner may have
                # already committed a fresh bundle.
                current = await get_connection_by_id(connection_id, decrypt=True)
                if current is None:
                    raise TokenUnavailable("unknown_connection")
                fresh_remaining = _expiry_seconds(current)
                if fresh_remaining is None or fresh_remaining > REFRESH_MARGIN_SECONDS:
                    return _token_view(current)
                return await _do_refresh(connection_id, current)
            finally:
                await cur.execute("SELECT pg_advisory_unlock(%s)", (key,))


async def _wait_for_winner(connection_id: str, row: dict) -> dict:
    """Loser path: old token if comfortably valid, else briefly poll the row."""
    if _usable(row, floor=OLD_TOKEN_FLOOR_SECONDS):
        return _token_view(row)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LOSER_POLL_SECONDS
    generation = row["token_generation"]
    while loop.time() < deadline:
        await asyncio.sleep(0.25)
        current = await get_connection_by_id(connection_id, decrypt=True)
        if current is None:
            raise TokenUnavailable("unknown_connection")
        if current["token_generation"] > generation and _usable(current):
            return _token_view(current)
    if _usable(row):
        return _token_view(row)
    raise TokenUnavailable("refresh_in_progress")


async def _do_refresh(connection_id: str, row: dict) -> dict:
    """Winner path: one refresh POST, generation-CAS commit."""
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
        data["client_secret"] = client_secret

    token_endpoint = (row.get("as_metadata") or {}).get("token_endpoint")
    if not token_endpoint:
        await mark_status(connection_id, "needs_reauth")
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
        await mark_status(connection_id, "refresh_ambiguous")
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
        body = (await response.aread())[:300]
        if response.status_code in (400, 401):
            logger.warning(
                "[mcp_oauth] refresh rejected for %s (%s): %s",
                connection_id, response.status_code, body,
            )
            await mark_status(connection_id, "needs_reauth")
            raise TokenUnavailable("needs_reauth", "refresh token rejected")
        # 5xx: transient server-side trouble — keep status, ride the old token.
        logger.warning(
            "[mcp_oauth] refresh %s for %s: %s",
            response.status_code, connection_id, body,
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
        refresh_token=token.refresh_token,  # None keeps the stored one
        expires_at=expires_at,
        scope=token.scope,
    )
    if not committed:
        # Lost the CAS (should not happen under the lock; defensive): serve
        # whatever is now current.
        current = await get_connection_by_id(connection_id, decrypt=True)
        if current and _usable(current):
            return _token_view(current)
        raise TokenUnavailable("refresh_in_progress")
    logger.info(
        "[mcp_oauth] refreshed connection %s (rotated_refresh=%s)",
        connection_id, token.refresh_token is not None,
    )
    return {
        "access_token": token.access_token,
        "token_type": token.token_type or "Bearer",
        "server_name": row["server_name"],
        "status": "connected",
    }


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
    await mark_status(row["connection_id"], "revoked")
    await revoke_grants_for_connection(row["connection_id"])
    await delete_user_tool_schemas(user_id, server_name)
    await bump_user_workspaces_mcp_version(user_id)
    logger.info(
        "[mcp_oauth] disconnected user=%s server=%s connection=%s",
        user_id, server_name, row["connection_id"],
    )
    return True
