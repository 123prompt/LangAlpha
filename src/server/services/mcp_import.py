"""Scope-neutral bulk import of standard ``mcpServers`` JSON.

Both import surfaces (per-workspace servers and the user-level Connectors
catalog) accept the same blob with inline credentials, and run the same
per-entry gauntlet: skip reserved/duplicate names, enforce the scope's cap,
rewrite credential-looking literals to ``${vault:NAME}`` refs, validate, then
persist — rolling the extracted secrets back whenever the entry fails after
extraction. That loop, the heuristic, and the rollback bookkeeping live here
once; a scope supplies only what genuinely differs (its cap, its prose, its
storage).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pydantic import ValidationError

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


@dataclass(frozen=True)
class ImportScope:
    """What one import surface contributes to the shared per-entry loop."""

    reserved_names: set[str]
    existing_names: set[str]
    # Rows already counted against ``cap`` (the workspace surface counts only
    # its OWN servers, not inherited/marker rows, so it can't be derived from
    # ``existing_names``).
    current_count: int
    cap: int
    cap_message: str
    exists_message: str
    # ``True`` when the server was created, ``False`` when the name turned out
    # to be taken; ``ValueError`` for a scope-level refusal (cap, raced dupe).
    persist: Callable[[Any], Awaitable[bool]]


@dataclass
class ImportReport:
    results: list[dict[str, Any]] = field(default_factory=list)
    created: int = 0
    secrets_created: list[str] = field(default_factory=list)


class ImportSession:
    """The vault side of one import run: storage plus its dedupe bookkeeping.

    ``allocated`` maps a literal value to the ref it became, so an identical
    token reused across servers is stored once; ``used_secret_names`` keeps
    allocated names unique against the vault's existing contents. Both unwind
    on rollback, so a failed entry can never leave a ref pointing at a deleted
    secret.
    """

    def __init__(
        self,
        *,
        create_secret: SecretCreate,
        delete_secret: SecretDelete,
        used_secret_names: set[str],
    ) -> None:
        self._create_secret = create_secret
        self._delete_secret = delete_secret
        self.used_secret_names = used_secret_names
        self.allocated: dict[str, str] = {}

    async def extract(self, server_name: str, config: dict[str, Any]) -> list[str]:
        return await extract_literals_to_vault(
            server_name,
            config,
            allocated=self.allocated,
            used_secret_names=self.used_secret_names,
            create_secret=self._create_secret,
            delete_secret=self._delete_secret,
        )

    async def rollback(self, names: list[str]) -> None:
        await rollback_import_secrets(
            names,
            allocated=self.allocated,
            used_secret_names=self.used_secret_names,
            delete_secret=self._delete_secret,
        )


async def run_mcp_import(
    parsed: list[Any], *, scope: ImportScope, session: ImportSession
) -> ImportReport:
    """Import each parsed entry into ``scope``, reporting per-entry outcomes.

    A partial import is the normal case: every entry that fails is reported in
    place (``invalid`` / ``skipped`` / ``exists`` / ``error``) and the rest
    continue, so one bad server never aborts the blob.
    """
    from src.server.models.mcp_server import (
        McpServerInput,
        _format_validation_error,
    )

    report = ImportReport()
    seen_names: set[str] = set()

    for entry in parsed:
        base = {
            "original_name": entry.original_name,
            "name": entry.name,
            "renamed": entry.renamed,
        }
        if entry.error:
            report.results.append({**base, "status": "invalid", "error": entry.error})
            continue
        if entry.name in scope.reserved_names:
            report.results.append(
                {**base, "status": "skipped", "reason": "collides with a built-in server"}
            )
            continue
        if entry.name in seen_names or entry.name in scope.existing_names:
            duplicate = entry.name in seen_names
            report.results.append({
                **base,
                "status": "skipped" if duplicate else "exists",
                "reason": (
                    "duplicate name after normalization"
                    if duplicate
                    else scope.exists_message
                ),
            })
            continue
        if scope.current_count + report.created >= scope.cap:
            report.results.append(
                {**base, "status": "error", "error": scope.cap_message}
            )
            continue

        seen_names.add(entry.name)
        config = dict(entry.config)
        try:
            made = await session.extract(entry.name, config)
        except ValueError as e:
            report.results.append({**base, "status": "error", "error": str(e)})
            continue

        # An authenticated remote server needs its header even to list tools, so
        # discovery must resolve secrets — set it explicitly so the stored value
        # (and the UI toggle) is honest (matches discovery_should_use_secrets).
        if config.get("transport") in ("http", "sse"):
            headers = config.get("headers") or {}
            if any(VAULT_REF_RE.search(str(v)) for v in headers.values()):
                config["discovery_uses_secrets"] = True

        try:
            server = McpServerInput(**config)
        except ValidationError as e:
            await session.rollback(made)
            report.results.append(
                {**base, "status": "invalid", "error": _format_validation_error(e)}
            )
            continue

        try:
            created = await scope.persist(server)
        except ValueError as e:
            await session.rollback(made)
            report.results.append({**base, "status": "error", "error": str(e)})
            continue
        if not created:
            await session.rollback(made)
            report.results.append({**base, "status": "exists"})
            continue

        report.secrets_created.extend(made)
        report.created += 1
        report.results.append({**base, "status": "created"})

    return report


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
    for section in ("env", "headers"):
        mapping = config.get(section)
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
        config[section] = out

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
