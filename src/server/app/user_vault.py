"""User-level Vault Secrets API Router (Connectors backing store).

CRUD for per-user encrypted secrets. These back inherited (source='user') MCP
servers the same way workspace secrets back workspace-local ones: at sandbox
push the two sets are merged, workspace winning on name collision. Mutations
push the merged set to the user's live sandboxes best-effort; every other
workspace converges on its next slow-path sync.

Endpoints:
- GET    /api/v1/mcp/vault/secrets
- POST   /api/v1/mcp/vault/secrets
- PUT    /api/v1/mcp/vault/secrets/{name}
- GET    /api/v1/mcp/vault/secrets/{name}/reveal
- DELETE /api/v1/mcp/vault/secrets/{name}
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from ptc_agent.core.mcp_sanitize import vault_refs
from src.server.app.vault import CreateSecretRequest, UpdateSecretRequest
from src.server.database.mcp_servers import (
    bump_user_workspaces_mcp_version,
    list_enabled_user_servers,
)
from src.server.database.user_vault_secrets import (
    MAX_SECRETS_PER_USER,
    create_user_secret,
    delete_user_secret,
    get_user_secrets,
    reveal_user_secret,
    update_user_secret,
)
from src.server.database.workspace import get_running_workspace_ids_for_user
from src.server.services.workspace_manager import WorkspaceManager
from src.server.utils.api import CurrentUserId, handle_api_exceptions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mcp", tags=["User Vault Secrets"])


def _server_vault_refs(row: dict) -> set[str]:
    """Vault names a user-server row actually references.

    Only env/headers/args/url are substituted at resolve time, so those are the
    only fields scanned — matching on the whole row would let a ``${vault:X}``
    string sitting in free-text description/instruction force a config bump.
    """
    refs: set[str] = set()
    for mapping in (row.get("env") or {}, row.get("headers") or {}):
        for value in mapping.values():
            refs.update(vault_refs(str(value)))
    for arg in row.get("args") or []:
        refs.update(vault_refs(str(arg)))
    refs.update(vault_refs(str(row.get("url") or "")))
    return refs


async def _after_mutation(user_id: str, name: str, *, value_changed: bool) -> None:
    """Best-effort convergence after a user-secret mutation.

    Pushes the merged secret set to the user's live sandboxes cached in THIS
    process (other workers converge on their next sync — the per-sync vault
    push already sends the merged set), and bumps the user's workspace config
    versions when the secret is referenced by an enabled inherited server so
    live sessions re-resolve it.
    """
    try:
        wm = WorkspaceManager.get_instance()
        for ws_id in await get_running_workspace_ids_for_user(user_id):
            await wm.push_vault_secrets(ws_id, user_id=user_id)
    except Exception:
        logger.warning(
            f"[user_vault] failed to push secrets to live sandboxes for {user_id}",
            exc_info=True,
        )

    if not value_changed:
        return
    try:
        referenced = any(
            name in _server_vault_refs(row)
            for row in await list_enabled_user_servers(user_id)
        )
        if referenced:
            await bump_user_workspaces_mcp_version(user_id)
            logger.info(
                f"[user_vault] secret {name!r} change bumped MCP config for "
                f"user {user_id}'s workspaces"
            )
    except Exception:
        logger.warning(
            f"[user_vault] MCP invalidation failed for user {user_id}",
            exc_info=True,
        )


@router.get("/vault/secrets")
@handle_api_exceptions("list user vault secrets", logger)
async def list_secrets(user_id: CurrentUserId):
    secrets = await get_user_secrets(user_id)
    return {
        "secrets": secrets,
        "remaining_slots": max(0, MAX_SECRETS_PER_USER - len(secrets)),
    }


@router.post("/vault/secrets", status_code=201)
@handle_api_exceptions("create user vault secret", logger)
async def create_secret(body: CreateSecretRequest, user_id: CurrentUserId):
    try:
        await create_user_secret(user_id, body.name, body.value, body.description)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    await _after_mutation(user_id, body.name, value_changed=True)
    return {"name": body.name}


@router.put("/vault/secrets/{name}")
@handle_api_exceptions("update user vault secret", logger)
async def update_secret(name: str, body: UpdateSecretRequest, user_id: CurrentUserId):
    found = await update_user_secret(
        user_id, name, value=body.value, description=body.description
    )
    if not found:
        raise HTTPException(status_code=404, detail="Secret not found")

    await _after_mutation(user_id, name, value_changed=body.value is not None)
    return {"name": name}


@router.get("/vault/secrets/{name}/reveal")
@handle_api_exceptions("reveal user vault secret", logger)
async def reveal_secret(name: str, user_id: CurrentUserId):
    value = await reveal_user_secret(user_id, name)
    if value is None:
        raise HTTPException(status_code=404, detail="Secret not found")
    return {"value": value}


@router.delete("/vault/secrets/{name}")
@handle_api_exceptions("delete user vault secret", logger)
async def delete_secret(name: str, user_id: CurrentUserId):
    found = await delete_user_secret(user_id, name)
    if not found:
        raise HTTPException(status_code=404, detail="Secret not found")

    await _after_mutation(user_id, name, value_changed=True)
    return {"ok": True}
