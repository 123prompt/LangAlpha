"""
Database layer for sandbox egress grants — the relay's contract.

A grant binds (user, workspace, credential) to one exact destination captured
at creation. The relay authorizes every request with one query here; grant or
connection status flips deny the next request with no sandbox convergence.
"""

import logging
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.encryption import get_encryption_key as _get_encryption_key
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

GRANT_KIND_OAUTH_MCP = "oauth_mcp"


async def ensure_oauth_grant(
    *,
    user_id: str,
    workspace_id: str,
    connection_id: str,
    destination_url: str,
    rate_class: str = "default",
) -> str:
    """Idempotently ensure the (workspace, connection) grant. Returns grant_id.

    Re-running refreshes destination_url and reactivates: the grant is
    workspace plumbing, not consent — consent lives on the connection, and a
    revoked/deleted connection cascades its grants away.
    """
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO sandbox_egress_grants
                    (user_id, workspace_id, kind, connection_id,
                     destination_url, rate_class, status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'active', NOW(), NOW())
                ON CONFLICT (workspace_id, kind, connection_id) DO UPDATE SET
                    destination_url = EXCLUDED.destination_url,
                    rate_class = EXCLUDED.rate_class,
                    status = 'active',
                    updated_at = NOW()
                RETURNING grant_id
                """,
                (
                    user_id, workspace_id, GRANT_KIND_OAUTH_MCP, connection_id,
                    destination_url, rate_class,
                ),
            )
            row = await cur.fetchone()
            return str(row["grant_id"])


async def list_workspace_oauth_grants(workspace_id: str) -> list[dict[str, Any]]:
    """Active grants + their connection identity, for turn-start sync/codegen."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT g.grant_id, g.user_id, g.connection_id, g.destination_url,
                       g.status AS grant_status,
                       c.server_name, c.status AS connection_status
                FROM sandbox_egress_grants g
                JOIN user_mcp_oauth_connections c ON c.connection_id = g.connection_id
                WHERE g.workspace_id = %s AND g.kind = %s AND g.status = 'active'
                ORDER BY c.server_name
                """,
                (workspace_id, GRANT_KIND_OAUTH_MCP),
            )
            rows = await cur.fetchall()
            return [
                {
                    "grant_id": str(r["grant_id"]),
                    "user_id": r["user_id"],
                    "connection_id": str(r["connection_id"]),
                    "destination_url": r["destination_url"],
                    "grant_status": r["grant_status"],
                    "server_name": r["server_name"],
                    "connection_status": r["connection_status"],
                }
                for r in rows
            ]


async def fetch_grant_for_relay(grant_id: str) -> dict[str, Any] | None:
    """The relay's per-request authorization + credential read, one roundtrip.

    Returns the grant with its connection's decrypted ACCESS token only —
    the refresh token is never selected here; refresh runs through the
    lifecycle service. None for an unknown grant_id (the route answers a
    uniform 404 for absent and wrong-scope alike).
    """
    enc_key = _get_encryption_key()
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT g.grant_id, g.user_id, g.workspace_id, g.kind,
                       g.connection_id, g.destination_url, g.allowed_methods,
                       g.tool_allowlist, g.policy_version, g.limits,
                       g.rate_class, g.status AS grant_status,
                       c.status AS connection_status, c.server_name,
                       c.token_type, c.expires_at, c.token_generation,
                       pgp_sym_decrypt(c.access_token, %s) AS access_token
                FROM sandbox_egress_grants g
                JOIN user_mcp_oauth_connections c ON c.connection_id = g.connection_id
                WHERE g.grant_id = %s
                """,
                (enc_key, grant_id),
            )
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "grant_id": str(row["grant_id"]),
                "user_id": row["user_id"],
                "workspace_id": str(row["workspace_id"]),
                "kind": row["kind"],
                "connection_id": str(row["connection_id"]),
                "destination_url": row["destination_url"],
                "allowed_methods": row["allowed_methods"],
                "tool_allowlist": row["tool_allowlist"],
                "policy_version": row["policy_version"],
                "limits": row["limits"] or {},
                "rate_class": row["rate_class"],
                "grant_status": row["grant_status"],
                "connection_status": row["connection_status"],
                "server_name": row["server_name"],
                "token_type": row["token_type"],
                "expires_at": row["expires_at"],
                "token_generation": row["token_generation"],
                "access_token": row["access_token"],
            }


async def revoke_grants_for_connection(connection_id: str) -> int:
    """Flip every grant of a connection to revoked. Returns count."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE sandbox_egress_grants
                SET status = 'revoked', updated_at = NOW()
                WHERE connection_id = %s AND status != 'revoked'
                """,
                (connection_id,),
            )
            if cur.rowcount:
                logger.info(
                    f"[egress_grants_db] revoked {cur.rowcount} grant(s) "
                    f"for connection {connection_id}"
                )
            return cur.rowcount


async def set_grant_policy(
    grant_id: str,
    *,
    tool_allowlist: list[str] | None,
    policy_version: int,
) -> bool:
    """Install/replace the tool allowlist (Part 2 populates this for curated
    connectors). None clears the policy (all tools pass)."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE sandbox_egress_grants
                SET tool_allowlist = %s, policy_version = %s, updated_at = NOW()
                WHERE grant_id = %s
                """,
                (
                    Json(tool_allowlist) if tool_allowlist is not None else None,
                    policy_version, grant_id,
                ),
            )
            return cur.rowcount == 1
