"""
Database layer for user MCP OAuth connections.

The token bundle is pgcrypto-encrypted; the refresh token never leaves this
table in any API response or sandbox artifact. token_generation increments on
every successful refresh — commit_refresh is compare-and-swap on it so two
workers can never both commit a refresh for the same generation (rotation
would otherwise destroy the surviving refresh token).

Statuses: connected | needs_reauth | refresh_ambiguous | revoked.
refresh_ambiguous means a refresh timed out ambiguously: the refresh token
may already be consumed server-side, so it must never be retried — the old
access token stays in use until expiry, then the connection needs re-auth.
"""

import logging
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.encryption import get_encryption_key as _get_encryption_key
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

CONNECTION_STATUSES = ("connected", "needs_reauth", "refresh_ambiguous", "revoked")


def _row_summary(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "connection_id": str(r["connection_id"]),
        "user_id": r["user_id"],
        "server_name": r["server_name"],
        "server_url": r["server_url"],
        "status": r["status"],
        "scope": r["scope"],
        "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
        "token_generation": r["token_generation"],
        "last_refresh_at": r["last_refresh_at"].isoformat() if r["last_refresh_at"] else None,
        "created_at": r["created_at"].isoformat(),
        "updated_at": r["updated_at"].isoformat(),
    }


async def upsert_connection(
    user_id: str,
    server_name: str,
    *,
    server_url: str,
    access_token: str,
    refresh_token: str | None,
    token_type: str = "Bearer",
    scope: str | None = None,
    expires_at: datetime | None = None,
    client_info: dict[str, Any] | None = None,
    client_secret: str | None = None,
    as_metadata: dict[str, Any] | None = None,
    resource_metadata: dict[str, Any] | None = None,
) -> str:
    """Store a freshly exchanged bundle (connect or re-auth). Returns connection_id.

    Re-auth on an existing row bumps token_generation like a refresh would —
    any caller pinned to the old generation sees rotation.
    """
    enc_key = _get_encryption_key()
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO user_mcp_oauth_connections
                    (user_id, server_name, server_url,
                     access_token, refresh_token, token_type, scope, expires_at,
                     token_generation, client_info, client_secret,
                     as_metadata, resource_metadata, status, created_at, updated_at)
                VALUES (%s, %s, %s,
                        pgp_sym_encrypt(%s, %s),
                        CASE WHEN %s::text IS NULL THEN NULL ELSE pgp_sym_encrypt(%s, %s) END,
                        %s, %s, %s,
                        0, %s,
                        CASE WHEN %s::text IS NULL THEN NULL ELSE pgp_sym_encrypt(%s, %s) END,
                        %s, %s, 'connected', NOW(), NOW())
                ON CONFLICT (user_id, server_name) DO UPDATE SET
                    server_url = EXCLUDED.server_url,
                    access_token = EXCLUDED.access_token,
                    refresh_token = EXCLUDED.refresh_token,
                    token_type = EXCLUDED.token_type,
                    scope = EXCLUDED.scope,
                    expires_at = EXCLUDED.expires_at,
                    token_generation = user_mcp_oauth_connections.token_generation + 1,
                    client_info = EXCLUDED.client_info,
                    client_secret = EXCLUDED.client_secret,
                    as_metadata = EXCLUDED.as_metadata,
                    resource_metadata = EXCLUDED.resource_metadata,
                    status = 'connected',
                    updated_at = NOW()
                RETURNING connection_id
                """,
                (
                    user_id, server_name, server_url,
                    access_token, enc_key,
                    refresh_token, refresh_token, enc_key,
                    token_type, scope, expires_at,
                    Json(client_info) if client_info is not None else None,
                    client_secret, client_secret, enc_key,
                    Json(as_metadata) if as_metadata is not None else None,
                    Json(resource_metadata) if resource_metadata is not None else None,
                ),
            )
            row = await cur.fetchone()
            logger.info(
                f"[mcp_oauth_db] upsert_connection user_id={user_id} server={server_name}"
            )
            return str(row["connection_id"])


async def get_connection(
    user_id: str, server_name: str, *, decrypt: bool = False
) -> dict[str, Any] | None:
    """Fetch one connection; decrypt=True adds the token bundle plaintext."""
    return await _fetch_one(
        "user_id = %s AND server_name = %s", (user_id, server_name), decrypt=decrypt
    )


async def get_connection_by_id(
    connection_id: str, *, decrypt: bool = False
) -> dict[str, Any] | None:
    return await _fetch_one("connection_id = %s", (connection_id,), decrypt=decrypt)


async def _fetch_one(
    where: str, params: tuple, *, decrypt: bool
) -> dict[str, Any] | None:
    enc_key = _get_encryption_key()
    secret_cols = (
        """,
               pgp_sym_decrypt(access_token, %s) AS access_token_plain,
               pgp_sym_decrypt(refresh_token, %s) AS refresh_token_plain,
               pgp_sym_decrypt(client_secret, %s) AS client_secret_plain
        """
        if decrypt
        else ""
    )
    query_params = ((enc_key, enc_key, enc_key) if decrypt else ()) + params
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT connection_id, user_id, server_name, server_url, status,
                       token_type, scope, expires_at, token_generation,
                       client_info, as_metadata, resource_metadata,
                       last_refresh_at, created_at, updated_at{secret_cols}
                FROM user_mcp_oauth_connections
                WHERE {where}
                """,
                query_params,
            )
            row = await cur.fetchone()
            if not row:
                return None
            out = _row_summary(row)
            out["client_info"] = row["client_info"]
            out["as_metadata"] = row["as_metadata"]
            out["resource_metadata"] = row["resource_metadata"]
            out["token_type"] = row["token_type"]
            if decrypt:
                out["access_token"] = row["access_token_plain"]
                out["refresh_token"] = row["refresh_token_plain"]
                out["client_secret"] = row["client_secret_plain"]
            return out


async def list_connections(user_id: str) -> list[dict[str, Any]]:
    """Status view for the UI. Never decrypts."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT connection_id, user_id, server_name, server_url, status,
                       scope, expires_at, token_generation, last_refresh_at,
                       created_at, updated_at
                FROM user_mcp_oauth_connections
                WHERE user_id = %s
                ORDER BY server_name
                """,
                (user_id,),
            )
            rows = await cur.fetchall()
            return [_row_summary(r) for r in rows]


async def commit_refresh(
    connection_id: str,
    *,
    expected_generation: int,
    access_token: str,
    refresh_token: str | None,
    expires_at: datetime | None,
    scope: str | None = None,
) -> bool:
    """Atomically commit a refresh iff the generation hasn't moved.

    refresh_token=None keeps the stored one (the AS didn't rotate it).
    Returns False when another worker already committed a newer generation —
    the caller must discard its result and re-read.
    """
    enc_key = _get_encryption_key()
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE user_mcp_oauth_connections SET
                    access_token = pgp_sym_encrypt(%s, %s),
                    refresh_token = CASE WHEN %s::text IS NULL
                        THEN refresh_token
                        ELSE pgp_sym_encrypt(%s, %s) END,
                    expires_at = %s,
                    scope = COALESCE(%s, scope),
                    token_generation = token_generation + 1,
                    status = 'connected',
                    last_refresh_at = NOW(),
                    updated_at = NOW()
                WHERE connection_id = %s
                  AND token_generation = %s
                  AND status IN ('connected', 'refresh_ambiguous')
                """,
                (
                    access_token, enc_key,
                    refresh_token, refresh_token, enc_key,
                    expires_at, scope,
                    connection_id, expected_generation,
                ),
            )
            committed = cur.rowcount == 1
            if committed:
                logger.info(
                    f"[mcp_oauth_db] commit_refresh connection_id={connection_id} "
                    f"generation={expected_generation + 1}"
                )
            return committed


async def mark_status(connection_id: str, status: str) -> bool:
    """Transition durable status. Tokens are left in place: refresh_ambiguous
    keeps serving the old access token until expiry, and needs_reauth keeps
    metadata for the reconnect flow."""
    if status not in CONNECTION_STATUSES:
        raise ValueError(f"invalid connection status {status!r}")
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE user_mcp_oauth_connections
                SET status = %s, updated_at = NOW()
                WHERE connection_id = %s
                """,
                (status, connection_id),
            )
            if cur.rowcount == 1:
                logger.info(
                    f"[mcp_oauth_db] mark_status connection_id={connection_id} status={status}"
                )
                return True
            return False


async def delete_connection(user_id: str, server_name: str) -> str | None:
    """Full disconnect. Cascade removes the connection's egress grants.
    Returns the deleted connection_id, or None."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                DELETE FROM user_mcp_oauth_connections
                WHERE user_id = %s AND server_name = %s
                RETURNING connection_id
                """,
                (user_id, server_name),
            )
            row = await cur.fetchone()
            if row:
                logger.info(
                    f"[mcp_oauth_db] delete_connection user_id={user_id} server={server_name}"
                )
                return str(row["connection_id"])
            return None


async def list_due_refresh(margin_seconds: int, limit: int = 25) -> list[dict[str, Any]]:
    """Sweeper scan: connected rows whose access token expires within the margin."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT connection_id, user_id, server_name, token_generation, expires_at
                FROM user_mcp_oauth_connections
                WHERE status = 'connected'
                  AND refresh_token IS NOT NULL
                  AND expires_at IS NOT NULL
                  AND expires_at < NOW() + make_interval(secs => %s)
                ORDER BY expires_at
                LIMIT %s
                """,
                (margin_seconds, limit),
            )
            rows = await cur.fetchall()
            return [
                {
                    "connection_id": str(r["connection_id"]),
                    "user_id": r["user_id"],
                    "server_name": r["server_name"],
                    "token_generation": r["token_generation"],
                    "expires_at": r["expires_at"],
                }
                for r in rows
            ]
