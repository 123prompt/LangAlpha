"""
Database layer for sandbox egress grants — the relay's contract.

A grant binds (user, workspace, credential) to one exact destination captured
at creation. The relay authorizes every request with one query here; grant or
connection status flips deny the next request with no sandbox convergence.
"""

import logging
from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

GRANT_KIND_OAUTH_MCP = "oauth_mcp"


class GrantConnectionUnavailable(Exception):
    """No connection backs the requested grant — absent, or another user's."""


async def ensure_oauth_grant(
    *,
    user_id: str,
    workspace_id: str,
    connection_id: str,
    destination_url: str,
) -> str:
    """Idempotently ensure the (workspace, connection) grant. Returns grant_id.

    Re-running refreshes destination_url and reactivates: the grant is
    workspace plumbing, not consent — consent lives on the connection, and a
    revoked/deleted connection cascades its grants away.

    The connection is selected rather than trusted, under the owner predicate:
    a connection_id that is absent or belongs to a different user matches no
    row, so it can never be bound into this workspace. Both cases raise
    :class:`GrantConnectionUnavailable` — a caller that guessed an id learns
    nothing from telling them apart.
    """
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO sandbox_egress_grants
                    (user_id, workspace_id, kind, connection_id,
                     destination_url, status, created_at, updated_at)
                SELECT %s, %s::uuid, %s, c.connection_id, %s, 'active',
                       NOW(), NOW()
                FROM user_mcp_oauth_connections c
                WHERE c.connection_id = %s::uuid AND c.user_id = %s
                ON CONFLICT (workspace_id, kind, connection_id) DO UPDATE SET
                    destination_url = EXCLUDED.destination_url,
                    status = 'active',
                    updated_at = NOW()
                RETURNING grant_id
                """,
                (
                    user_id, workspace_id, GRANT_KIND_OAUTH_MCP, destination_url,
                    connection_id, user_id,
                ),
            )
            row = await cur.fetchone()
            if row is None:
                raise GrantConnectionUnavailable(
                    f"no OAuth connection {connection_id} for this user"
                )
            return str(row["grant_id"])


async def fetch_grant_for_relay(grant_id: str) -> dict[str, Any] | None:
    """The relay's per-request authorization read.

    Authorization only — no credential. The vendor token comes from the OAuth
    lifecycle (which owns refresh and the generation CAS), so this stays a
    cheap non-decrypting read on the hot path. None for an unknown grant_id
    (the route answers a uniform 404 for absent and wrong-scope alike).
    """
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT g.user_id, g.workspace_id, g.connection_id,
                       g.destination_url, g.allowed_methods, g.tool_allowlist,
                       g.status AS grant_status,
                       c.status AS connection_status
                FROM sandbox_egress_grants g
                JOIN user_mcp_oauth_connections c ON c.connection_id = g.connection_id
                WHERE g.grant_id = %s
                """,
                (grant_id,),
            )
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "user_id": row["user_id"],
                "workspace_id": str(row["workspace_id"]),
                "connection_id": str(row["connection_id"]),
                "destination_url": row["destination_url"],
                "allowed_methods": row["allowed_methods"],
                "tool_allowlist": row["tool_allowlist"],
                "grant_status": row["grant_status"],
                "connection_status": row["connection_status"],
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
