"""Discovery schema cache for MCP tool snapshots — workspace and user tiers.

Both tables have the same shape and are keyed by ``(owner, server_name,
config_hash)``: a per-server config fingerprint, so adding or toggling an
unrelated server never orphans a snapshot. The SQL therefore lives here once,
parameterized by a ``_SchemaTier`` descriptor — the tiers differ only in the
owner column and in the user tier's extra ``schema_digest``, which lets OAuth
fan-out fire only when tool content actually changed.

Snapshot reads are deliberately decoupled from ``workspaces.mcp_config_version``
(that lives in ``mcp_servers``): the caller compares a row's ``config_hash``
against the server's CURRENT fingerprint to decide hit vs. stale.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.pool import get_db_connection


# SQL identifiers can never be bound parameters, so a tier's table/column names
# are f-string interpolated into the statements below. These allowlists are what
# makes that safe: a tier is only constructible from names that exist here.
_ALLOWED_TABLES = frozenset({"workspace_mcp_tool_schemas", "user_mcp_tool_schemas"})
_ALLOWED_COLUMNS = frozenset({"workspace_id", "user_id"})


@dataclass(frozen=True)
class _SchemaTier:
    """The identifiers that distinguish one snapshot tier from the other."""

    table: str
    owner_col: str
    has_digest: bool

    def __post_init__(self) -> None:
        if self.table not in _ALLOWED_TABLES:
            raise ValueError(f"Unknown schema table: {self.table!r}")
        if self.owner_col not in _ALLOWED_COLUMNS:
            raise ValueError(f"Unknown schema column: {self.owner_col!r}")

    @property
    def columns(self) -> tuple[str, ...]:
        """Full column list, in INSERT order (``discovered_at`` last)."""
        digest = ("schema_digest",) if self.has_digest else ()
        return (
            self.owner_col, "server_name", "config_hash", "tools", "status",
            "error", *digest, "observed_meta", "discovered_at",
        )


WORKSPACE_TIER = _SchemaTier("workspace_mcp_tool_schemas", "workspace_id", False)
USER_TIER = _SchemaTier("user_mcp_tool_schemas", "user_id", True)

# A non-ok write must never downgrade an existing same-hash ``ok`` row: the
# config is unchanged, so the cached tools are still valid. Only ``error`` is
# taken from the failing write.
_DOWNGRADE = "t.status = 'ok' AND EXCLUDED.status <> 'ok'"


def _keep_on_downgrade(col: str, fresh: str = "") -> str:
    return f"{col} = CASE WHEN {_DOWNGRADE} THEN t.{col} ELSE {fresh or f'EXCLUDED.{col}'} END"


# ---------------------------------------------------------------------------
# Tier-parameterized implementations
# ---------------------------------------------------------------------------


async def _latest(tier: _SchemaTier, owner_id: str) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT DISTINCT ON (server_name) {", ".join(tier.columns)}
                FROM {tier.table}
                WHERE {tier.owner_col} = %s
                ORDER BY server_name, discovered_at DESC
                """,
                (owner_id,),
            )
            return [_row_to_dict(tier, r) for r in await cur.fetchall()]


async def _upsert(
    tier: _SchemaTier,
    owner_id: str,
    server_name: str,
    config_hash: str,
    *,
    tools: list[dict[str, Any]] | None,
    status: str,
    error: str,
    schema_digest: str,
    observed_meta: dict[str, Any] | None,
) -> dict[str, Any]:
    columns = tier.columns
    values = [owner_id, server_name, config_hash, Json(tools or []), status, error]
    if tier.has_digest:
        values.append(schema_digest)
    values.append(Json(observed_meta or {}))
    updates = [
        _keep_on_downgrade("tools"),
        _keep_on_downgrade("status"),
        "error = EXCLUDED.error",
        *([_keep_on_downgrade("schema_digest")] if tier.has_digest else []),
        _keep_on_downgrade("observed_meta"),
        _keep_on_downgrade("discovered_at", "NOW()"),
    ]
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                # Only the current config's snapshot is kept, so iterating on a
                # server's config doesn't accumulate dead rows.
                await cur.execute(
                    f"""
                    DELETE FROM {tier.table}
                    WHERE {tier.owner_col} = %s AND server_name = %s
                      AND config_hash <> %s
                    """,
                    (owner_id, server_name, config_hash),
                )
                await cur.execute(
                    f"""
                    INSERT INTO {tier.table} AS t ({", ".join(columns)})
                    VALUES ({", ".join(["%s"] * len(values))}, NOW())
                    ON CONFLICT ({tier.owner_col}, server_name, config_hash)
                        DO UPDATE SET {", ".join(updates)}
                    RETURNING {", ".join(columns)}
                    """,
                    values,
                )
                return _row_to_dict(tier, await cur.fetchone())


async def _delete(
    tier: _SchemaTier, owner_id: str, server_name: str, *, conn=None
) -> int:
    async with get_db_connection(conn) as db:
        async with db.cursor() as cur:
            await cur.execute(
                f"DELETE FROM {tier.table} "
                f"WHERE {tier.owner_col} = %s AND server_name = %s",
                (owner_id, server_name),
            )
            return cur.rowcount


async def _delete_and_bump(
    tier: _SchemaTier, owner_id: str, server_names: list[str]
) -> int:
    # Imported here, not at module scope: ``mcp_servers`` re-exports this
    # module's public names, so a top-level import would close the cycle.
    from src.server.database.mcp_servers import _bump_user_versions, _bump_version

    bump = _bump_version if tier is WORKSPACE_TIER else _bump_user_versions
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    f"DELETE FROM {tier.table} "
                    f"WHERE {tier.owner_col} = %s AND server_name = ANY(%s)",
                    (owner_id, server_names),
                )
                deleted = cur.rowcount
                await bump(cur, owner_id)
                return deleted


def _row_to_dict(tier: _SchemaTier, row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        tier.owner_col: str(row[tier.owner_col]),
        "server_name": row["server_name"],
        "config_hash": row["config_hash"],
        "tools": row["tools"] or [],
        "status": row["status"],
        "error": row["error"] or "",
        "observed_meta": row["observed_meta"] or {},
        "discovered_at": row["discovered_at"].isoformat(),
    }
    if tier.has_digest:
        out["schema_digest"] = row["schema_digest"] or ""
    return out


# ---------------------------------------------------------------------------
# Workspace tier — public API
# ---------------------------------------------------------------------------


async def get_tool_schemas(workspace_id: str) -> list[dict[str, Any]]:
    return await _latest(WORKSPACE_TIER, workspace_id)


async def upsert_tool_schemas(
    workspace_id: str,
    server_name: str,
    config_hash: str,
    *,
    tools: list[dict[str, Any]] | None = None,
    status: str = "pending",
    error: str = "",
    observed_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await _upsert(
        WORKSPACE_TIER, workspace_id, server_name, config_hash,
        tools=tools, status=status, error=error, schema_digest="",
        observed_meta=observed_meta,
    )


async def delete_tool_schemas(workspace_id: str, server_name: str) -> int:
    """Drop a server's snapshots at EVERY hash — for invalidation that the
    config fingerprint can't see, e.g. a vault secret discovery depends on
    changing value."""
    return await _delete(WORKSPACE_TIER, workspace_id, server_name)


async def delete_tool_schemas_and_bump(
    workspace_id: str, server_names: list[str]
) -> int:
    """Purge workspace snapshots for the named servers AND bump the config
    version, atomically — a mid-purge failure must never leave schemas
    partially deleted with the version un-bumped (live sessions would then skip
    re-resolution against the half-purged cache)."""
    return await _delete_and_bump(WORKSPACE_TIER, workspace_id, server_names)


# ---------------------------------------------------------------------------
# User tier — public API (host-side discovery for OAuth servers)
# ---------------------------------------------------------------------------


async def get_user_tool_schemas(user_id: str) -> list[dict[str, Any]]:
    return await _latest(USER_TIER, user_id)


async def upsert_user_tool_schemas(
    user_id: str,
    server_name: str,
    config_hash: str,
    *,
    tools: list[dict[str, Any]] | None = None,
    status: str = "pending",
    error: str = "",
    schema_digest: str = "",
    observed_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await _upsert(
        USER_TIER, user_id, server_name, config_hash,
        tools=tools, status=status, error=error, schema_digest=schema_digest,
        observed_meta=observed_meta,
    )


async def delete_user_tool_schemas(
    user_id: str, server_name: str, *, conn=None
) -> int:
    return await _delete(USER_TIER, user_id, server_name, conn=conn)


async def delete_user_tool_schemas_and_bump(
    user_id: str, server_names: list[str]
) -> int:
    """User-tier twin of ``delete_tool_schemas_and_bump``: purge the named
    servers' snapshots and fan the version bump out to every workspace of the
    user, in one transaction."""
    return await _delete_and_bump(USER_TIER, user_id, server_names)
