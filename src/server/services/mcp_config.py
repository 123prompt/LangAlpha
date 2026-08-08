"""Per-workspace MCP configuration resolution — the single chokepoint.

Modeled on ``resolve_llm_config``. Merges the process-global built-in MCP
servers (from ``base_config.mcp.servers``), the user's enabled user-level
servers, and a workspace's DB-backed rows into one deterministic effective set:

    effective = built-ins (config order)
                MINUS names disabled by a (source='builtin', enabled=false) row
                PLUS  enabled user-level servers (alphabetical)
                MINUS names disabled by a (source='user', enabled=false) row
                MINUS names shadowed by a workspace-local server
                PLUS  source='workspace' enabled rows (alphabetical, appended)

Collision policy: built-in names are reserved (a user or workspace server can
never shadow one); a workspace-local server shadows an inherited user server
of the same name (the explicit local-fork affordance).

User-level mutations bump every workspace of the user (one transaction), so
the single per-workspace ``mcp_config_version`` remains the only drift signal
sessions have to watch.

The merged list and the DB↔model converters are defined ONCE here so the API
effective-list endpoint and the sandbox-sync path can import the same logic
(no prompt/wrapper divergence).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ptc_agent.config.core import MCPServerConfig

# Same hard-coded logger name request_prep uses — existing log routing keys off it.
logger = logging.getLogger("src.server.handlers.chat_handler")


# Vault-reference resolution (``${vault:NAME}``) happens in-sandbox in Phase 2,
# not here — this module is the merge/convert chokepoint only. The canonical
# pattern lives in ``ptc_agent.core.mcp_sanitize.VAULT_REF_RE`` (Lane A); the
# Phase 2 secret-resolution codegen should import it from there.


@dataclass(frozen=True)
class ResolvedMCP:
    """The effective MCP server set for one workspace at one config version.

    ``servers`` is deterministic (built-ins in config order, then user servers
    alphabetical). ``builtin_names`` / ``user_names`` partition the effective
    set by origin. ``disabled_builtin_names`` lists built-ins removed by a
    disable-marker row, and ``disabled_workspace_servers`` lists disabled
    user servers — both are excluded from ``servers`` (so they don't run) but
    carried so the API can keep a re-enable toggle in the UI. ``version`` is
    ``workspaces.mcp_config_version``.
    """

    servers: list[MCPServerConfig]
    builtin_names: frozenset[str]
    user_names: frozenset[str]
    version: int
    disabled_builtin_names: frozenset[str] = frozenset()
    disabled_workspace_servers: list[MCPServerConfig] = field(default_factory=list)
    # User-level (workspace-inherited) layer. inherited_names partitions the
    # effective set alongside builtin_names/user_names (which keeps its
    # historical meaning: workspace-LOCAL server names). Tombstoned = removed
    # from this workspace by a (source='user', enabled=false) marker; shadowed
    # = hidden behind a workspace-local server of the same name. Both carry
    # full configs so the UI can render them with their toggle.
    inherited_names: frozenset[str] = frozenset()
    tombstoned_inherited_servers: list[MCPServerConfig] = field(default_factory=list)
    shadowed_inherited_names: frozenset[str] = frozenset()
    # server_name → OAuth connection status, INCLUDING revoked (unlike the
    # per-server ``oauth_connection_id``, which only binds live connections).
    # Lets consumers tell "never OAuth" from "OAuth but disconnected" — the
    # effective-list API surfaces it and discovery skips such servers (an
    # in-sandbox probe of a token-less OAuth server can only fail).
    oauth_status_by_name: dict[str, str] = field(default_factory=dict)


def workspace_row_to_server_config(row: dict) -> MCPServerConfig:
    """Convert a ``workspace_mcp_servers`` row into an ``MCPServerConfig``.

    Defined ONCE; imported by the API and sandbox-sync lanes. ``source`` is
    forced to ``"workspace"`` and any stored ``vault_blueprints`` key is
    stripped (defense in depth — user servers never declare blueprints).
    """
    config = dict(row.get("config") or {})
    config.pop("vault_blueprints", None)
    config.pop("source", None)  # never trust a stored source tag
    # The row's name is authoritative over any name baked into the JSON blob.
    config["name"] = row["name"]
    config["source"] = "workspace"
    config["enabled"] = bool(row.get("enabled", True))
    return MCPServerConfig(**config)


def user_row_to_server_config(
    row: dict, *, oauth_connection_id: str | None = None
) -> MCPServerConfig:
    """Convert a ``user_mcp_servers`` row (flat columns, no config blob) into
    an ``MCPServerConfig`` with ``source='user'``."""
    return MCPServerConfig(
        name=row["name"],
        enabled=True,
        description=row.get("description") or "",
        instruction=row.get("instruction") or "",
        transport=row.get("transport") or "stdio",
        command=row.get("command"),
        args=row.get("args") or [],
        env=row.get("env") or {},
        url=row.get("url"),
        headers=row.get("headers") or {},
        tool_exposure_mode=row.get("tool_exposure_mode") or None,
        source="user",
        discovery_uses_secrets=bool(row.get("discovery_uses_secrets", False)),
        oauth_connection_id=oauth_connection_id,
    )


async def resolve_mcp_config(
    base_config,
    user_id: str,
    workspace_id: str,
) -> ResolvedMCP:
    """Resolve the effective MCP server set for ``workspace_id``.

    Built-ins come from ``base_config.mcp.servers`` (enabled ones, config
    order); a ``(source='builtin', enabled=false)`` row removes a built-in by
    name; enabled user-level servers are inherited (alphabetical) unless
    tombstoned by a ``(source='user', enabled=false)`` row or shadowed by a
    workspace-local server; ``source='workspace'`` enabled rows are appended
    alphabetically. A workspace with zero rows AND zero inherited servers
    returns the built-in objects unchanged (no copies) so the common case
    stays byte-identical downstream.
    """
    import asyncio

    from src.server.database.mcp_oauth import list_connections
    from src.server.database.mcp_servers import (
        get_workspace_servers_and_version,
        list_enabled_user_servers,
    )

    # Built-ins from the global config, enabled only, in declaration order.
    builtin_servers = [
        s for s in base_config.mcp.servers
        if getattr(s, "enabled", True)
    ]
    builtin_name_set = {s.name for s in builtin_servers}

    # Version is read BEFORE the rows (READ COMMITTED, not a snapshot) so a
    # concurrent mutation can only skew toward (older version, newer rows) —
    # the live version then exceeds what we cache and the next acquire
    # re-resolves. The reverse pairing would cache stale rows under the new
    # version and stick. See get_workspace_servers_and_version. User-level
    # mutations fan the bump out to every workspace of the user, so the same
    # ordering argument covers the user reads below.
    rows, version = await get_workspace_servers_and_version(workspace_id)
    user_rows, connections = await asyncio.gather(
        list_enabled_user_servers(user_id),
        list_connections(user_id),
    )

    # Short-circuit: nothing user-level and no workspace rows ⇒ the effective
    # set IS the built-in list (same objects, no copies).
    if not rows and not user_rows:
        return ResolvedMCP(
            servers=builtin_servers,
            builtin_names=frozenset(builtin_name_set),
            user_names=frozenset(),
            version=version,
        )

    connection_by_server = {
        c["server_name"]: c for c in connections if c["status"] != "revoked"
    }
    oauth_status_by_name = {c["server_name"]: str(c["status"]) for c in connections}

    disabled_builtins: set[str] = set()
    tombstoned_user_names: set[str] = set()
    local_servers: list[MCPServerConfig] = []
    disabled_local_servers: list[MCPServerConfig] = []
    local_names: set[str] = set()
    for row in rows:
        if row["source"] == "builtin":
            # Disable-marker: only acts when it turns a built-in off.
            if not row["enabled"]:
                disabled_builtins.add(row["name"])
            continue
        if row["source"] == "user":
            # Tombstone: removes an inherited user server from THIS workspace.
            if not row["enabled"]:
                tombstoned_user_names.add(row["name"])
            continue
        # source == 'workspace'
        if row["name"] in builtin_name_set:
            # Backstop for the API's 409: a user server must never collide with
            # a built-in name. Skip + log; do not let it shadow the built-in.
            logger.warning(
                "[MCP] Skipping workspace server %r in workspace %s: name "
                "collides with a built-in (API should reject at write).",
                row["name"], workspace_id,
            )
            continue
        try:
            cfg = workspace_row_to_server_config(row)
        except Exception:
            logger.error(
                "[MCP] Failed to parse workspace server %r in workspace %s; "
                "skipping.", row["name"], workspace_id, exc_info=True,
            )
            continue
        # A workspace-local row shadows an inherited user server of the same
        # name whether enabled or not — a disabled local fork must not fall
        # back to running the inherited config the user explicitly forked.
        local_names.add(cfg.name)
        # Disabled workspace servers are excluded from the effective set (they
        # don't run), but carried separately so the API keeps a re-enable
        # toggle in the UI — mirrors disabled_builtin_names for built-ins.
        if row["enabled"]:
            local_servers.append(cfg)
        else:
            disabled_local_servers.append(cfg)

    inherited_servers: list[MCPServerConfig] = []
    tombstoned_inherited: list[MCPServerConfig] = []
    shadowed_inherited: set[str] = set()
    for row in user_rows:
        name = row["name"]
        if name in builtin_name_set:
            # Built-in names are reserved at the user level too.
            logger.warning(
                "[MCP] Skipping user server %r for user %s: name collides "
                "with a built-in (API should reject at write).", name, user_id,
            )
            continue
        connection = connection_by_server.get(name)
        try:
            cfg = user_row_to_server_config(
                row,
                oauth_connection_id=(
                    connection["connection_id"] if connection else None
                ),
            )
        except Exception:
            logger.error(
                "[MCP] Failed to parse user server %r for user %s; skipping.",
                name, user_id, exc_info=True,
            )
            continue
        if name in local_names:
            shadowed_inherited.add(name)
        elif name in tombstoned_user_names:
            tombstoned_inherited.append(cfg)
        else:
            inherited_servers.append(cfg)

    effective_builtins = [
        s for s in builtin_servers if s.name not in disabled_builtins
    ]
    inherited_servers.sort(key=lambda s: s.name)
    tombstoned_inherited.sort(key=lambda s: s.name)
    local_servers.sort(key=lambda s: s.name)
    disabled_local_servers.sort(key=lambda s: s.name)

    return ResolvedMCP(
        servers=[*effective_builtins, *inherited_servers, *local_servers],
        builtin_names=frozenset(s.name for s in effective_builtins),
        user_names=frozenset(s.name for s in local_servers),
        version=version,
        disabled_builtin_names=frozenset(disabled_builtins & builtin_name_set),
        disabled_workspace_servers=disabled_local_servers,
        inherited_names=frozenset(s.name for s in inherited_servers),
        tombstoned_inherited_servers=tombstoned_inherited,
        shadowed_inherited_names=frozenset(shadowed_inherited),
        oauth_status_by_name=oauth_status_by_name,
    )
