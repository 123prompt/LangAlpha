"""
Database CRUD for user-level vault secrets.

Same pgcrypto-at-rest pattern as workspace_vault_secrets; user secrets are
merged with workspace secrets at sandbox push, workspace winning on name
collision.
"""

import logging
from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection
from src.server.database.encryption import get_encryption_key as _get_encryption_key
from src.server.database.vault_secrets import _mask

logger = logging.getLogger(__name__)

MAX_SECRETS_PER_USER = 50


async def get_user_secrets(user_id: str) -> list[dict[str, Any]]:
    """List all secrets for a user (decrypted server-side for masking)."""
    enc_key = _get_encryption_key()
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT user_vault_secret_id, name, description,
                       pgp_sym_decrypt(value, %s) AS plaintext,
                       created_at, updated_at
                FROM user_vault_secrets
                WHERE user_id = %s
                ORDER BY name
                """,
                (enc_key, user_id),
            )
            rows = await cur.fetchall()
            return [
                {
                    "user_vault_secret_id": str(r["user_vault_secret_id"]),
                    "name": r["name"],
                    "description": r["description"] or "",
                    "masked_value": _mask(r["plaintext"]),
                    "created_at": r["created_at"].isoformat(),
                    "updated_at": r["updated_at"].isoformat(),
                }
                for r in rows
            ]


async def get_user_secrets_decrypted(user_id: str) -> dict[str, str]:
    """Return {name: plaintext_value} for sandbox injection."""
    enc_key = _get_encryption_key()
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT name, pgp_sym_decrypt(value, %s) AS plaintext
                FROM user_vault_secrets
                WHERE user_id = %s
                """,
                (enc_key, user_id),
            )
            rows = await cur.fetchall()
            return {r["name"]: r["plaintext"] for r in rows}


async def get_user_secret_names(user_id: str) -> set[str]:
    """Return the set of secret names for a user. No decryption."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT name FROM user_vault_secrets WHERE user_id = %s",
                (user_id,),
            )
            rows = await cur.fetchall()
            return {r["name"] for r in rows}


async def create_user_secret(
    user_id: str, name: str, value: str, description: str = ""
) -> None:
    """Insert a new secret (encrypted). Raises ValueError on duplicate or limit."""
    enc_key = _get_encryption_key()
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                # Serialize concurrent creates for the same user
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (user_id,),
                )
                await cur.execute(
                    "SELECT COUNT(*) AS cnt FROM user_vault_secrets WHERE user_id = %s",
                    (user_id,),
                )
                row = await cur.fetchone()
                if row["cnt"] >= MAX_SECRETS_PER_USER:
                    raise ValueError(
                        f"Maximum of {MAX_SECRETS_PER_USER} secrets per user reached"
                    )

                await cur.execute(
                    """
                    INSERT INTO user_vault_secrets
                        (user_id, name, value, description, created_at, updated_at)
                    VALUES (%s, %s, pgp_sym_encrypt(%s, %s), %s, NOW(), NOW())
                    ON CONFLICT (user_id, name) DO NOTHING
                    RETURNING user_vault_secret_id
                    """,
                    (user_id, name, value, enc_key, description),
                )
                inserted = await cur.fetchone()
                if not inserted:
                    raise ValueError(
                        f"Secret with name {name!r} already exists"
                    )
                logger.info(
                    f"[user_vault_db] create_secret user_id={user_id} name={name}"
                )


async def update_user_secret(
    user_id: str,
    name: str,
    *,
    value: str | None = None,
    description: str | None = None,
) -> bool:
    """Partial update of a secret. Returns True if row was found."""
    if value is None and description is None:
        return True  # nothing to update

    enc_key = _get_encryption_key()
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            parts: list[str] = []
            params: list[Any] = []
            if value is not None:
                parts.append("value = pgp_sym_encrypt(%s, %s)")
                params.extend([value, enc_key])
            if description is not None:
                parts.append("description = %s")
                params.append(description)
            parts.append("updated_at = NOW()")
            params.extend([user_id, name])

            await cur.execute(
                f"UPDATE user_vault_secrets SET {', '.join(parts)} "
                "WHERE user_id = %s AND name = %s",
                params,
            )
            if cur.rowcount == 0:
                return False
            logger.info(
                f"[user_vault_db] update_secret user_id={user_id} name={name}"
            )
            return True


async def delete_user_secret(user_id: str, name: str) -> bool:
    """Delete a secret by name. Returns True if row existed."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM user_vault_secrets WHERE user_id = %s AND name = %s",
                (user_id, name),
            )
            if cur.rowcount == 0:
                return False
            logger.info(
                f"[user_vault_db] delete_secret user_id={user_id} name={name}"
            )
            return True
