"""Database CRUD for per-workspace and user-level MCP server configuration.

Two concerns live here:
- User-level servers (``user_mcp_servers``): CRUD by ``(user_id, name)``.
  ``enabled`` rows are LIVE config inherited by every workspace of the user at
  resolve time; disabled rows are inert templates (the pre-connectors
  behavior). Any mutation of an enabled row fans out a version bump to ALL the
  user's workspaces in the same transaction — convergence is next-acquire.
- Per-workspace rows (``workspace_mcp_servers``): the source of truth for a
  workspace's effective MCP set. EVERY write bumps ``workspaces.mcp_config_version``
  in the SAME transaction so sessions can detect drift on their next acquire.

The discovery schema cache for both tiers lives in ``mcp_tool_schemas``.

Secrets are never stored here — env/header values hold ``${vault:NAME}``
references resolved against ``workspace_vault_secrets`` inside the sandbox.
"""

import logging
from collections.abc import Mapping
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

# Hard cap on user-configured (source='workspace') servers per workspace.
MAX_MCP_SERVERS_PER_WORKSPACE = 20

# Hard cap on catalog templates per user.
MAX_CATALOG_SERVERS_PER_USER = 50

# Mutable catalog columns, split by how a value binds. Anything outside the
# union is rejected by ``update_catalog_server`` rather than silently dropped.
_CATALOG_JSONB_COLUMNS = frozenset({"args", "env", "headers"})
_CATALOG_SCALAR_COLUMNS = frozenset({
    "transport", "command", "url", "description", "instruction",
    "tool_exposure_mode", "discovery_uses_secrets",
})
CATALOG_COLUMNS = _CATALOG_JSONB_COLUMNS | _CATALOG_SCALAR_COLUMNS


# ---------------------------------------------------------------------------
# User-level catalog (templates)
# ---------------------------------------------------------------------------


async def list_catalog_servers(user_id: str) -> list[dict[str, Any]]:
    """List all catalog templates for a user, ordered by name."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT user_mcp_server_id, user_id, name, transport, command, args,
                       url, env, headers, description, instruction, tool_exposure_mode,
                       discovery_uses_secrets, enabled, created_at, updated_at
                FROM user_mcp_servers
                WHERE user_id = %s
                ORDER BY name
                """,
                (user_id,),
            )
            return [_catalog_row_to_dict(r) for r in await cur.fetchall()]


async def get_catalog_server(user_id: str, name: str) -> dict[str, Any] | None:
    """Return a single catalog template by name, or None."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT user_mcp_server_id, user_id, name, transport, command, args,
                       url, env, headers, description, instruction, tool_exposure_mode,
                       discovery_uses_secrets, enabled, created_at, updated_at
                FROM user_mcp_servers
                WHERE user_id = %s AND name = %s
                """,
                (user_id, name),
            )
            row = await cur.fetchone()
            return _catalog_row_to_dict(row) if row else None


async def create_catalog_server(
    user_id: str,
    name: str,
    *,
    transport: str = "stdio",
    command: str | None = None,
    args: list[str] | None = None,
    url: str | None = None,
    env: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    description: str = "",
    instruction: str = "",
    tool_exposure_mode: str = "summary",
    discovery_uses_secrets: bool = False,
    conn=None,
) -> dict[str, Any]:
    """Insert a catalog template. Raises ValueError on duplicate name or over cap.

    Enforces ``MAX_CATALOG_SERVERS_PER_USER`` under an advisory lock on the
    user so concurrent creates can't slip past the cap.
    """
    async with get_db_connection(conn) as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                # Serialize concurrent catalog creates for this user.
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s::text))",
                    (user_id,),
                )
                await cur.execute(
                    "SELECT COUNT(*) AS cnt FROM user_mcp_servers "
                    "WHERE user_id = %s AND name <> %s",
                    (user_id, name),
                )
                cnt = (await cur.fetchone())["cnt"]
                if cnt >= MAX_CATALOG_SERVERS_PER_USER:
                    raise ValueError(
                        f"Maximum of {MAX_CATALOG_SERVERS_PER_USER} "
                        "MCP catalog servers per user reached"
                    )

                await cur.execute(
                    """
                    INSERT INTO user_mcp_servers
                        (user_id, name, transport, command, args, url, env, headers,
                         description, instruction, tool_exposure_mode,
                         discovery_uses_secrets, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (user_id, name) DO NOTHING
                    RETURNING user_mcp_server_id, user_id, name, transport, command, args,
                              url, env, headers, description, instruction, tool_exposure_mode,
                              discovery_uses_secrets, enabled, created_at, updated_at
                    """,
                    (
                        user_id, name, transport, command, Json(args or []), url,
                        Json(env or {}), Json(headers or {}), description, instruction,
                        tool_exposure_mode, discovery_uses_secrets,
                    ),
                )
                row = await cur.fetchone()
                if not row:
                    raise ValueError(
                        f"MCP catalog server {name!r} already exists for this user"
                    )
                logger.info(f"[mcp_db] create_catalog_server user_id={user_id} name={name}")
                return _catalog_row_to_dict(row)


async def update_catalog_server(
    user_id: str, name: str, *, updates: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Partial update of a catalog template. Returns the row, or None if absent.

    Raises ValueError on a key outside ``CATALOG_COLUMNS``: a caller that
    misspells a column must not have the write silently dropped.
    """
    unknown = sorted(set(updates) - CATALOG_COLUMNS)
    if unknown:
        raise ValueError(f"unknown catalog column(s): {', '.join(unknown)}")
    if not updates:
        return await get_catalog_server(user_id, name)

    parts: list[str] = [f"{col} = %s" for col in updates]
    params: list[Any] = [
        Json(val) if col in _CATALOG_JSONB_COLUMNS else val
        for col, val in updates.items()
    ]
    parts.append("updated_at = NOW()")
    params.extend([user_id, name])

    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"UPDATE user_mcp_servers SET {', '.join(parts)} "
                    "WHERE user_id = %s AND name = %s "
                    "RETURNING user_mcp_server_id, user_id, name, transport, command, args, "
                    "url, env, headers, description, instruction, tool_exposure_mode, "
                    "discovery_uses_secrets, enabled, created_at, updated_at",
                    params,
                )
                row = await cur.fetchone()
                if not row:
                    return None
                # A live (enabled) server changed shape — every workspace of the
                # user must re-resolve on next acquire.
                if row["enabled"]:
                    await _bump_user_versions(cur, user_id)
                logger.info(f"[mcp_db] update_catalog_server user_id={user_id} name={name}")
                return _catalog_row_to_dict(row)


async def delete_catalog_server(user_id: str, name: str) -> bool:
    """Delete a user server by name. Returns True if a row existed.

    For an enabled (live) server, the same transaction also purges its
    per-workspace disable-markers (their name would otherwise squat the
    UNIQUE(workspace_id, name) slot forever), drops the user-level discovery
    cache, and fans out the version bump.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "DELETE FROM user_mcp_servers WHERE user_id = %s AND name = %s "
                    "RETURNING enabled",
                    (user_id, name),
                )
                row = await cur.fetchone()
                if not row:
                    return False
                await cur.execute(
                    """
                    DELETE FROM workspace_mcp_servers
                    WHERE name = %s AND source = 'user' AND workspace_id IN
                        (SELECT workspace_id FROM workspaces WHERE user_id = %s)
                    """,
                    (name, user_id),
                )
                await cur.execute(
                    "DELETE FROM user_mcp_tool_schemas "
                    "WHERE user_id = %s AND server_name = %s",
                    (user_id, name),
                )
                if row["enabled"]:
                    await _bump_user_versions(cur, user_id)
                logger.info(f"[mcp_db] delete_catalog_server user_id={user_id} name={name}")
                return True


async def set_catalog_server_enabled(
    user_id: str, name: str, enabled: bool
) -> dict[str, Any] | None:
    """Toggle a user server live/inert. Returns the row, or None if absent.

    Both directions change every workspace's effective set, so the fan-out
    bump always runs in the same transaction.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    UPDATE user_mcp_servers
                    SET enabled = %s, updated_at = NOW()
                    WHERE user_id = %s AND name = %s
                    RETURNING user_mcp_server_id, user_id, name, transport, command, args,
                              url, env, headers, description, instruction, tool_exposure_mode,
                              discovery_uses_secrets, enabled, created_at, updated_at
                    """,
                    (enabled, user_id, name),
                )
                row = await cur.fetchone()
                if not row:
                    return None
                await _bump_user_versions(cur, user_id)
                logger.info(
                    f"[mcp_db] set_catalog_server_enabled user_id={user_id} "
                    f"name={name} enabled={enabled}"
                )
                return _catalog_row_to_dict(row)


async def list_enabled_user_servers(user_id: str) -> list[dict[str, Any]]:
    """Enabled (live) user servers, for the resolve-time merge."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT user_mcp_server_id, user_id, name, transport, command, args,
                       url, env, headers, description, instruction, tool_exposure_mode,
                       discovery_uses_secrets, enabled, created_at, updated_at
                FROM user_mcp_servers
                WHERE user_id = %s AND enabled = TRUE
                ORDER BY name
                """,
                (user_id,),
            )
            return [_catalog_row_to_dict(r) for r in await cur.fetchall()]


async def bump_user_workspaces_mcp_version(user_id: str) -> int:
    """Bump mcp_config_version on ALL of a user's workspaces (own transaction).

    For out-of-band user-level invalidation (OAuth connect/disconnect, user
    vault changes referenced by live servers). Returns workspaces touched.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await _bump_user_versions(cur, user_id)
            return cur.rowcount


# ---------------------------------------------------------------------------
# Per-workspace rows (source of truth) — every write bumps mcp_config_version
# ---------------------------------------------------------------------------


async def list_workspace_servers(workspace_id: str) -> list[dict[str, Any]]:
    """List all MCP rows for a workspace (both disable-markers and user servers)."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT workspace_mcp_server_id, workspace_id, name, source, enabled,
                       config, created_at, updated_at
                FROM workspace_mcp_servers
                WHERE workspace_id = %s
                ORDER BY name
                """,
                (workspace_id,),
            )
            return [_workspace_row_to_dict(r) for r in await cur.fetchall()]


async def get_workspace_servers_and_version(
    workspace_id: str,
) -> tuple[list[dict[str, Any]], int]:
    """Read a workspace's mcp_config_version then its MCP rows. Order matters.

    The shared connection is READ COMMITTED, so the two SELECTs are not one
    snapshot; a mutation (rows + version bump in one txn) can land between them.
    Reading the version FIRST bounds the only possible skew to (older version,
    newer rows) — safe, because the live version is then higher than what the
    caller caches, so its next acquire re-resolves and self-corrects. The reverse
    order would cache stale rows under the new version, and the matching version
    would short-circuit re-resolve, making the drift stick.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT mcp_config_version FROM workspaces WHERE workspace_id = %s",
                    (workspace_id,),
                )
                ws = await cur.fetchone()
                await cur.execute(
                    """
                    SELECT workspace_mcp_server_id, workspace_id, name, source, enabled,
                           config, created_at, updated_at
                    FROM workspace_mcp_servers
                    WHERE workspace_id = %s
                    ORDER BY name
                    """,
                    (workspace_id,),
                )
                rows = [_workspace_row_to_dict(r) for r in await cur.fetchall()]
    version = int((ws or {}).get("mcp_config_version") or 0)
    return rows, version


async def upsert_workspace_server(
    workspace_id: str,
    name: str,
    *,
    source: str,
    enabled: bool,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert or update a workspace MCP row; bumps mcp_config_version in the txn.

    On insert of a new ``source='workspace'`` row, enforces
    ``MAX_MCP_SERVERS_PER_WORKSPACE`` under an advisory lock so concurrent
    creates can't slip past the cap. Disable-markers (``source='builtin'``)
    do not count against the cap.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                # Serialize concurrent mutations for this workspace.
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s::text))",
                    (workspace_id,),
                )
                if source == "workspace":
                    await cur.execute(
                        """
                        SELECT COUNT(*) AS cnt FROM workspace_mcp_servers
                        WHERE workspace_id = %s AND source = 'workspace'
                          AND name <> %s
                        """,
                        (workspace_id, name),
                    )
                    cnt = (await cur.fetchone())["cnt"]
                    if cnt >= MAX_MCP_SERVERS_PER_WORKSPACE:
                        raise ValueError(
                            f"Maximum of {MAX_MCP_SERVERS_PER_WORKSPACE} "
                            "MCP servers per workspace reached"
                        )

                await cur.execute(
                    """
                    INSERT INTO workspace_mcp_servers
                        (workspace_id, name, source, enabled, config, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (workspace_id, name) DO UPDATE
                        SET source = EXCLUDED.source,
                            enabled = EXCLUDED.enabled,
                            config = EXCLUDED.config,
                            updated_at = NOW()
                    RETURNING workspace_mcp_server_id, workspace_id, name, source,
                              enabled, config, created_at, updated_at
                    """,
                    (
                        workspace_id, name, source, enabled,
                        Json(config) if config is not None else None,
                    ),
                )
                row = await cur.fetchone()
                await _bump_version(cur, workspace_id)
                logger.info(
                    f"[mcp_db] upsert_workspace_server workspace_id={workspace_id} "
                    f"name={name} source={source} enabled={enabled}"
                )
                return _workspace_row_to_dict(row)


async def insert_workspace_server(
    workspace_id: str,
    name: str,
    *,
    config: dict[str, Any] | None = None,
    conn=None,
) -> dict[str, Any] | None:
    """Insert a NEW source='workspace' row; bumps version. None on name conflict.

    Uses ``ON CONFLICT DO NOTHING`` so a concurrent create of the same new name
    can't silently turn into an UPDATE (last-write-wins). Returns None when the
    name already exists, which the router maps to a 409. Enforces
    ``MAX_MCP_SERVERS_PER_WORKSPACE`` under the same advisory lock as upsert.
    """
    async with get_db_connection(conn) as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                # Serialize concurrent mutations for this workspace.
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s::text))",
                    (workspace_id,),
                )
                await cur.execute(
                    """
                    SELECT COUNT(*) AS cnt FROM workspace_mcp_servers
                    WHERE workspace_id = %s AND source = 'workspace'
                    """,
                    (workspace_id,),
                )
                cnt = (await cur.fetchone())["cnt"]
                if cnt >= MAX_MCP_SERVERS_PER_WORKSPACE:
                    raise ValueError(
                        f"Maximum of {MAX_MCP_SERVERS_PER_WORKSPACE} "
                        "MCP servers per workspace reached"
                    )

                await cur.execute(
                    """
                    INSERT INTO workspace_mcp_servers
                        (workspace_id, name, source, enabled, config, created_at, updated_at)
                    VALUES (%s, %s, 'workspace', TRUE, %s, NOW(), NOW())
                    ON CONFLICT (workspace_id, name) DO NOTHING
                    RETURNING workspace_mcp_server_id, workspace_id, name, source,
                              enabled, config, created_at, updated_at
                    """,
                    (
                        workspace_id, name,
                        Json(config) if config is not None else None,
                    ),
                )
                row = await cur.fetchone()
                if row is None:
                    # Name already exists ⇒ conflict; don't bump version.
                    return None
                await _bump_version(cur, workspace_id)
                logger.info(
                    f"[mcp_db] insert_workspace_server workspace_id={workspace_id} "
                    f"name={name}"
                )
                return _workspace_row_to_dict(row)


async def set_workspace_server_enabled(
    workspace_id: str, name: str, enabled: bool
) -> bool:
    """Toggle a workspace MCP row's enabled flag; bumps version. False if absent."""
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s::text))",
                    (workspace_id,),
                )
                await cur.execute(
                    "UPDATE workspace_mcp_servers SET enabled = %s, updated_at = NOW() "
                    "WHERE workspace_id = %s AND name = %s",
                    (enabled, workspace_id, name),
                )
                if cur.rowcount == 0:
                    return False
                await _bump_version(cur, workspace_id)
                logger.info(
                    f"[mcp_db] set_workspace_server_enabled workspace_id={workspace_id} "
                    f"name={name} enabled={enabled}"
                )
                return True


async def delete_workspace_server(workspace_id: str, name: str) -> bool:
    """Delete a workspace MCP row; bumps version. False if no row existed."""
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s::text))",
                    (workspace_id,),
                )
                await cur.execute(
                    "DELETE FROM workspace_mcp_servers "
                    "WHERE workspace_id = %s AND name = %s",
                    (workspace_id, name),
                )
                if cur.rowcount == 0:
                    return False
                await cur.execute(
                    "DELETE FROM workspace_mcp_tool_schemas "
                    "WHERE workspace_id = %s AND server_name = %s",
                    (workspace_id, name),
                )
                await _bump_version(cur, workspace_id)
                logger.info(
                    f"[mcp_db] delete_workspace_server workspace_id={workspace_id} "
                    f"name={name}"
                )
                return True


async def bump_workspace_mcp_version(workspace_id: str) -> None:
    """Bump mcp_config_version outside a row mutation (own transaction).

    For out-of-band invalidation (vault secret changes) where no
    ``workspace_mcp_servers`` row is written but live sessions must re-resolve.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await _bump_version(cur, workspace_id)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _bump_version(cur, workspace_id: str) -> None:
    """Atomically increment a workspace's mcp_config_version (same txn)."""
    await cur.execute(
        "UPDATE workspaces SET mcp_config_version = mcp_config_version + 1 "
        "WHERE workspace_id = %s",
        (workspace_id,),
    )


async def _bump_user_versions(cur, user_id: str) -> None:
    """Increment mcp_config_version on every workspace of a user (same txn).

    One statement, unpaginated on purpose: a user-level change must never
    leave a subset of workspaces on the old version.
    """
    await cur.execute(
        "UPDATE workspaces SET mcp_config_version = mcp_config_version + 1 "
        "WHERE user_id = %s",
        (user_id,),
    )


def _catalog_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a user_mcp_servers row into a plain JSON-friendly dict."""
    return {
        "user_mcp_server_id": str(row["user_mcp_server_id"]),
        "user_id": row["user_id"],
        "name": row["name"],
        "transport": row["transport"],
        "command": row["command"],
        "args": row["args"] or [],
        "url": row["url"],
        "env": row["env"] or {},
        "headers": row["headers"] or {},
        "description": row["description"] or "",
        "instruction": row["instruction"] or "",
        "tool_exposure_mode": row["tool_exposure_mode"],
        "discovery_uses_secrets": bool(row.get("discovery_uses_secrets", False)),
        "enabled": bool(row.get("enabled", False)),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _workspace_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a workspace_mcp_servers row into a plain dict."""
    return {
        "workspace_mcp_server_id": str(row["workspace_mcp_server_id"]),
        "workspace_id": str(row["workspace_id"]),
        "name": row["name"],
        "source": row["source"],
        "enabled": row["enabled"],
        "config": row["config"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }
