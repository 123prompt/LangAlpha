"""Scope-neutral literal→vault extraction for MCP server bulk imports.

Both import surfaces (per-workspace servers and the user-level Connectors
catalog) accept standard ``mcpServers`` JSON with inline credentials; this
module rewrites credential-looking literals to ``${vault:NAME}`` refs through
caller-supplied secret storage callables, so the heuristic and the rollback
bookkeeping live once.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable

from ptc_agent.core.mcp_sanitize import VAULT_REF_RE

logger = logging.getLogger(__name__)

# ``(name, value, description)`` — raises ValueError on duplicate/cap.
SecretCreate = Callable[[str, str, str], Awaitable[Any]]
# ``(name)`` — best-effort delete.
SecretDelete = Callable[[str], Awaitable[Any]]

# On bulk import, an env/header value is auto-extracted into a vault secret when
# it looks like a credential — either the key name reads like one, or the value
# is a long opaque token. Benign config (``MODE=prod``, ``LOG_LEVEL=ERROR``)
# stays an inline literal so we don't clutter the vault.
_SECRET_KEY_RE = re.compile(
    r"(?i)(secret|token|password|passwd|pwd|apikey|api[_-]?key|access[_-]?key|"
    r"authorization|auth|bearer|credential|cred|private[_-]?key|\bpat\b|\bkey\b)"
)
_OPAQUE_TOKEN_MIN_LEN = 20


def looks_like_secret(key: str, value: str) -> bool:
    """Heuristic: should this env/header literal be vaulted rather than inlined?"""
    if _SECRET_KEY_RE.search(key or ""):
        return True
    v = value or ""
    return len(v) >= _OPAQUE_TOKEN_MIN_LEN and " " not in v and not v.isdigit()


def vault_secret_name(server_name: str, key: str, used: set[str]) -> str:
    """Allocate a unique, NAME_RE-legal vault secret name for ``server.key``."""
    base = re.sub(r"[^A-Za-z0-9_]", "_", f"{server_name}_{key}".upper())
    if base and base[0].isdigit():
        base = f"_{base}"
    base = base[:64] or "IMPORTED_SECRET"
    name = base
    i = 2
    while name in used:
        suffix = f"_{i}"
        name = f"{base[: 64 - len(suffix)]}{suffix}"
        i += 1
    return name


async def rollback_import_secrets(
    names: list[str],
    *,
    allocated: dict[str, str],
    used_secret_names: set[str],
    delete_secret: SecretDelete,
) -> None:
    """Best-effort removal of vault secrets created for a server whose import failed.

    Also unwinds the cross-server dedupe bookkeeping so a later server in the
    same import can't reuse a ref that points at a deleted secret.
    """
    if not names:
        return
    refs = {f"${{vault:{n}}}" for n in names}
    for literal in [k for k, v in allocated.items() if v in refs]:
        del allocated[literal]
    for n in names:
        used_secret_names.discard(n)
        try:
            await delete_secret(n)
        except Exception:
            logger.warning(
                "[mcp] failed to roll back imported secret %s", n, exc_info=True
            )


async def extract_literals_to_vault(
    server_name: str,
    config: dict[str, Any],
    *,
    allocated: dict[str, str],
    used_secret_names: set[str],
    create_secret: SecretCreate,
    delete_secret: SecretDelete,
) -> list[str]:
    """Move credential-looking env/header literals into the vault, in place.

    Existing ``${vault:NAME}`` refs and benign config literals are left alone.
    Returns the names of any vault secrets created. May raise ``ValueError`` if
    the vault secret cap is reached — secrets already created for THIS server
    are rolled back first, so a cap hit never strands orphans.
    """
    created: list[str] = []
    try:
        return await _extract_literals_inner(
            server_name,
            config,
            allocated=allocated,
            used_secret_names=used_secret_names,
            created=created,
            create_secret=create_secret,
        )
    except Exception:
        await rollback_import_secrets(
            created,
            allocated=allocated,
            used_secret_names=used_secret_names,
            delete_secret=delete_secret,
        )
        raise


async def _extract_literals_inner(
    server_name: str,
    config: dict[str, Any],
    *,
    allocated: dict[str, str],
    used_secret_names: set[str],
    created: list[str],
    create_secret: SecretCreate,
) -> list[str]:
    for field in ("env", "headers"):
        mapping = config.get(field)
        if not isinstance(mapping, dict):
            continue
        out: dict[str, Any] = {}
        for k, v in mapping.items():
            if (
                not isinstance(v, str)
                or not v.strip()
                or VAULT_REF_RE.fullmatch(v)
                or not looks_like_secret(str(k), v)
            ):
                out[k] = v
                continue
            ref = allocated.get(v)
            if ref is None:
                secret_name = vault_secret_name(server_name, str(k), used_secret_names)
                await create_secret(
                    secret_name, v, f"Imported with MCP server {server_name}"
                )
                used_secret_names.add(secret_name)
                created.append(secret_name)
                ref = f"${{vault:{secret_name}}}"
                allocated[v] = ref
            out[k] = ref
        config[field] = out

    # stdio ``args`` is a list; the common credential shape is a single
    # ``--flag=VALUE`` token (or ``KEY=VALUE``). Split on the first ``=`` and
    # vault the value half when the flag or value looks secret, rewriting the arg
    # to ``--flag=${vault:NAME}`` (the generated client resolves refs in args).
    # Bare / space-separated arg secrets (``--token VALUE``) are left as-is —
    # too ambiguous to auto-extract without over-vaulting benign positionals.
    args = config.get("args")
    if isinstance(args, list):
        new_args: list[Any] = []
        for arg in args:
            if not isinstance(arg, str) or "=" not in arg:
                new_args.append(arg)
                continue
            flag, _, val = arg.partition("=")
            if (
                not val.strip()
                or VAULT_REF_RE.search(val)
                or not looks_like_secret(flag, val)
            ):
                new_args.append(arg)
                continue
            ref = allocated.get(val)
            if ref is None:
                key_hint = flag.lstrip("-") or "arg"
                secret_name = vault_secret_name(server_name, key_hint, used_secret_names)
                await create_secret(
                    secret_name, val, f"Imported with MCP server {server_name}"
                )
                used_secret_names.add(secret_name)
                created.append(secret_name)
                ref = f"${{vault:{secret_name}}}"
                allocated[val] = ref
            new_args.append(f"{flag}={ref}")
        config["args"] = new_args
    return created
