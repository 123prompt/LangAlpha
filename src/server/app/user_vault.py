"""User-level Vault Secrets API Router (Connectors backing store).

CRUD for per-user encrypted secrets. These back inherited (source='user') MCP
servers the same way workspace secrets back workspace-local ones: at sandbox
push the two sets are merged, workspace winning on name collision. Convergence
after a mutation is ``services/vault_invalidation`` — the same code the
workspace tier runs, entered with the user tier's descriptor.

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

from src.server.database.user_vault_secrets import (
    MAX_SECRETS_PER_USER,
    create_user_secret,
    delete_user_secret,
    get_user_secrets,
    reveal_user_secret,
    update_user_secret,
)
from src.server.models.vault import CreateSecretRequest, UpdateSecretRequest
from src.server.services.vault_invalidation import USER_TIER, after_secret_change
from src.server.utils.api import CurrentUserId, handle_api_exceptions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mcp", tags=["User Vault Secrets"])


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

    await after_secret_change(USER_TIER, user_id, body.name, user_id=user_id)
    return {"name": body.name}


@router.put("/vault/secrets/{name}")
@handle_api_exceptions("update user vault secret", logger)
async def update_secret(name: str, body: UpdateSecretRequest, user_id: CurrentUserId):
    found = await update_user_secret(
        user_id, name, value=body.value, description=body.description
    )
    if not found:
        raise HTTPException(status_code=404, detail="Secret not found")

    await after_secret_change(
        USER_TIER, user_id, name,
        user_id=user_id,
        value_changed=body.value is not None,
    )
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

    await after_secret_change(USER_TIER, user_id, name, user_id=user_id)
    return {"ok": True}
