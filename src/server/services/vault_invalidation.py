"""Convergence after a vault-secret mutation, shared by both vault tiers.

A mutation changes secret VALUES, and every config fingerprint in the MCP
machinery hashes ``${vault:NAME}`` reference strings rather than values — so
nothing downstream can see the change on its own. This module is the explicit
compensation, and it is one module rather than a block per router because the
workspace and user tiers differ only in which rows they scan, which caches they
purge, and which workspaces they converge.

Every step is best-effort: a failure here must never fail the mutation that
triggered it, only delay convergence to the next acquire.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ptc_agent.config.core import MCPServerConfig
from ptc_agent.core.mcp_sanitize import discovery_should_use_secrets, vault_refs
from src.server.database.mcp_servers import (
    bump_user_workspaces_mcp_version,
    bump_workspace_mcp_version,
    list_enabled_user_servers,
    list_workspace_servers,
)
from src.server.database.mcp_tool_schemas import (
    delete_tool_schemas_and_bump,
    delete_user_tool_schemas_and_bump,
)
from src.server.database.workspace import get_running_workspace_ids_for_user
from src.server.services.mcp_config import (
    user_row_to_server_config,
    workspace_row_to_server_config,
)
from src.server.services.workspace_manager import WorkspaceManager

logger = logging.getLogger(__name__)


def refs_for_server(server: MCPServerConfig) -> set[str]:
    """Vault names a server actually substitutes at resolve time.

    Only env/headers/args/url are substituted, so those are the only fields
    scanned — matching on the whole stored config would let a ``${vault:X}``
    string sitting in free-text description/instruction force a config bump.
    """
    refs: set[str] = set()
    for mapping in (server.env or {}, server.headers or {}):
        for value in mapping.values():
            refs.update(vault_refs(str(value)))
    for arg in server.args or []:
        refs.update(vault_refs(str(arg)))
    refs.update(vault_refs(str(server.url or "")))
    return refs


async def _workspace_servers(workspace_id: str) -> list[MCPServerConfig]:
    return _to_configs(
        [
            row
            for row in await list_workspace_servers(workspace_id)
            if row.get("source") == "workspace" and row.get("config")
        ],
        workspace_row_to_server_config,
    )


async def _user_servers(user_id: str) -> list[MCPServerConfig]:
    return _to_configs(await list_enabled_user_servers(user_id), user_row_to_server_config)


def _to_configs(
    rows: list[dict], convert: Callable[[dict], MCPServerConfig]
) -> list[MCPServerConfig]:
    out: list[MCPServerConfig] = []
    for row in rows:
        try:
            out.append(convert(row))
        except Exception:
            continue  # unparseable stored row: it can't be resolved either
    return out


@dataclass(frozen=True)
class VaultTier:
    """What distinguishes one vault tier's convergence from the other's."""

    label: str
    log_prefix: str
    servers: Callable[[str], Awaitable[list[MCPServerConfig]]]
    purge_and_bump: Callable[[str, list[str]], Awaitable[int]]
    bump: Callable[[str], Awaitable[object]]
    # Workspaces to push to and re-apply. The user tier fans out only to the
    # RUNNING ones: a user secret is inherited by every workspace, and warming
    # each idle sandbox to deliver it would be a cold-start storm.
    workspaces: Callable[[str], Awaitable[list[str]]]


async def _own_workspace(workspace_id: str) -> list[str]:
    return [workspace_id]


WORKSPACE_TIER = VaultTier(
    label="workspace",
    log_prefix="[vault]",
    servers=_workspace_servers,
    purge_and_bump=delete_tool_schemas_and_bump,
    bump=bump_workspace_mcp_version,
    workspaces=_own_workspace,
)

USER_TIER = VaultTier(
    label="user",
    log_prefix="[user_vault]",
    servers=_user_servers,
    purge_and_bump=delete_user_tool_schemas_and_bump,
    bump=bump_user_workspaces_mcp_version,
    workspaces=get_running_workspace_ids_for_user,
)


async def after_secret_change(
    tier: VaultTier,
    owner_id: str,
    secret_name: str,
    *,
    user_id: str,
    value_changed: bool = True,
) -> None:
    """Push the new secret set to live sandboxes and invalidate MCP caches.

    ``value_changed`` is False for a description-only edit: nothing a server
    resolves has moved, so the cache half is skipped.
    """
    await _push_secrets(tier, owner_id, user_id)
    if value_changed:
        await _invalidate_mcp(tier, owner_id, secret_name, user_id)


async def _push_secrets(tier: VaultTier, owner_id: str, user_id: str) -> None:
    """Push the merged secret set to whichever sandboxes are live in THIS
    process; other workers converge on their next sync (which pushes too)."""
    try:
        wm = WorkspaceManager.get_instance()
        for workspace_id in await tier.workspaces(owner_id):
            await wm.push_vault_secrets(workspace_id, user_id=user_id)
    except Exception:
        logger.warning(
            f"{tier.log_prefix} failed to push secrets for {tier.label} {owner_id}",
            exc_info=True,
        )


async def _invalidate_mcp(
    tier: VaultTier, owner_id: str, secret_name: str, user_id: str
) -> None:
    """Bump the config version for servers referencing the changed secret, purge
    the discovery snapshots that could depend on its value, and schedule a
    proactive apply so a ``needs_secret``/``pending`` server comes alive without
    waiting for the user's next message."""
    try:
        referencing = [
            server
            for server in await tier.servers(owner_id)
            if secret_name in refs_for_server(server)
        ]
        if not referencing:
            return

        # Only servers whose discovery runs WITH secrets can have a cached
        # tools/list that depends on the credential.
        purge = [s.name for s in referencing if discovery_should_use_secrets(s)]

        # Purge + bump in ONE transaction: a partial purge with an un-bumped
        # version would let live sessions skip re-resolution against the
        # half-purged cache.
        if purge:
            await tier.purge_and_bump(owner_id, purge)
        else:
            await tier.bump(owner_id)

        # Lazy: the scheduler lives in a router, and a service must not import
        # an app module at import time.
        from src.server.app.mcp_servers import _schedule_proactive_apply

        for workspace_id in await tier.workspaces(owner_id):
            _schedule_proactive_apply(workspace_id, user_id)
        logger.info(
            f"{tier.log_prefix} secret {secret_name!r} change invalidated MCP config "
            f"for {tier.label} {owner_id} ({len(referencing)} referencing server(s))"
        )
    except Exception:
        logger.warning(
            f"{tier.log_prefix} MCP invalidation failed for {tier.label} {owner_id}",
            exc_info=True,
        )
