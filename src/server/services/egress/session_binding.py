"""Egress-relay binding for one workspace session: grants, JWT, credential file.

The sandbox's only relay credential is a short-lived JWT plus a server→grant
map, written to a single file (`upload_egress_relay_credentials`). This module
owns the lifecycle of that file across the resolve path (`sync_egress_relay`)
and the warm fast path (`maybe_remint_egress_jwt`).

Multi-worker contract: the `sandbox_egress_grants` table is the truth about
which grants exist — `EgressBinding` on the session is execution context only
(what THIS process last pushed), so a worker that never bound anything still
converges removals by reading the table. No cross-worker lock guards the
credential-file push: the upload writes atomically (temp + same-dir rename in
`upload_egress_relay_credentials`), so two workers pushing the same workspace
concurrently can only ever leave one complete file — last rename wins, both
relay JWTs are valid, and the grant map converges on the next push.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.server.database.egress_grants import (
    GrantConnectionUnavailable,
    ensure_oauth_grant,
    retire_stale_grants,
)

if TYPE_CHECKING:
    from ptc_agent.core.session import Session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EgressBinding:
    """What this process last pushed to the sandbox.

    ``jwt_exp`` (from the mint, never recomputed) drives the cheap remint
    check; ``user_id`` lets the remint run on paths with no request user in
    hand. Not liveness truth — the grant rows are.
    """

    grants: Mapping[str, str]
    jwt_exp: float
    user_id: str


async def sync_egress_relay(
    workspace_id: str,
    user_id: str | None,
    session: "Session",
    resolved: Any,
) -> None:
    """Converge grants + relay JWT + sandbox credential file to ``resolved``.

    One grant per OAuth-connected server in the resolved set; grants the
    workspace no longer resolves are retired in the table (they are an
    authorization overhang otherwise — the sandbox may still hold their ids
    and a live JWT). Removal of the last OAuth server also deletes the
    credential file, decided from the table so it converges on any worker.
    """
    oauth_servers = [
        s for s in resolved.servers if getattr(s, "oauth_connection_id", None)
    ]
    sandbox = session.sandbox
    if not oauth_servers:
        retired = await retire_stale_grants(workspace_id, keep_grant_ids=())
        if retired or session.egress_binding is not None:
            session.egress_binding = None
            if sandbox is not None:
                await sandbox.upload_egress_relay_credentials(None)
        return

    from src.config.env import EGRESS_RELAY_SECRET

    if not EGRESS_RELAY_SECRET:
        logger.warning(
            "[EGRESS] OAuth-connected MCP servers %s present but "
            "EGRESS_RELAY_SECRET is unset — they stay unbound",
            [s.name for s in oauth_servers],
        )
        return
    if not user_id:
        # OAuth connections only resolve for authenticated users.
        return

    grants: dict[str, str] = {}
    for srv in oauth_servers:
        try:
            # No destination_url: the grant's dial target is the connection's
            # consented server_url, pinned inside ensure_oauth_grant. srv.url
            # (the mutable catalog URL) must never reach the grant.
            grant_id = await ensure_oauth_grant(
                user_id=user_id,
                workspace_id=workspace_id,
                connection_id=srv.oauth_connection_id,
            )
        except GrantConnectionUnavailable:
            # The connection vanished between resolve and here (disconnect
            # race): leave this one server unbound, keep binding the rest.
            logger.warning(
                "[EGRESS] connection %s gone for server %s — left unbound",
                srv.oauth_connection_id, srv.name,
            )
            continue
        srv.egress_grant_id = grant_id
        grants[srv.name] = grant_id

    await retire_stale_grants(
        workspace_id, keep_grant_ids=tuple(grants.values())
    )
    await _push_credentials(workspace_id, session, user_id, grants)


async def maybe_remint_egress_jwt(workspace_id: str, session: "Session") -> None:
    """Re-push credentials when the relay JWT nears expiry.

    Runs on the warm-cooldown path (which skips the resolve entirely), so a
    long-lived session keeps a valid JWT without re-resolving config. Pure
    in-memory compare unless the token is actually near expiry.
    """
    binding = session.egress_binding
    if binding is None:
        return
    from src.server.services.egress.relay_jwt import needs_remint

    if not needs_remint(binding.jwt_exp):
        return
    from src.config.env import EGRESS_RELAY_SECRET

    if not EGRESS_RELAY_SECRET:
        return
    try:
        await _push_credentials(
            workspace_id, session, binding.user_id, dict(binding.grants)
        )
        logger.info("[EGRESS] relay JWT reminted for workspace %s", workspace_id)
    except Exception as e:
        logger.warning(
            "[EGRESS] relay JWT remint failed for %s: %s", workspace_id, e
        )


async def _push_credentials(
    workspace_id: str,
    session: "Session",
    user_id: str,
    grants: dict[str, str],
) -> None:
    """Mint a fresh relay JWT and (re)write the sandbox credential file.

    No cross-worker lock: the upload replaces the file atomically, so a
    concurrent push from another worker can at worst overwrite this one's file
    with an equally-valid credential — never tear it.
    """
    from src.config.env import EGRESS_RELAY_SECRET
    from src.server.services.egress.reachability import (
        effective_relay_base_url,
        relay_reachability_warning,
    )
    from src.server.services.egress.relay_jwt import mint_relay_jwt

    sandbox = session.sandbox
    if sandbox is None:
        return
    provider = session.config.sandbox.provider
    relay_base = effective_relay_base_url(provider)
    warning = relay_reachability_warning(provider, relay_base)
    if warning:
        logger.warning("[EGRESS] %s", warning)
    minted = mint_relay_jwt(
        EGRESS_RELAY_SECRET,
        user_id=user_id,
        workspace_id=workspace_id,
        sandbox_id=getattr(sandbox, "sandbox_id", None) or "",
    )
    published = await sandbox.upload_egress_relay_credentials(
        {
            "relay_base_url": relay_base.rstrip("/"),
            "token": minted.token,
            "grants": grants,
        }
    )
    if not published:
        # The sandbox never got the fresh token. Leave the binding untouched so
        # the near-expiry check keeps firing (or, on a first push, stays unbound
        # and re-resolves) instead of trusting a token the sandbox can't see.
        logger.warning(
            "[EGRESS] credential push failed for workspace %s — binding unchanged",
            workspace_id,
        )
        return
    session.egress_binding = EgressBinding(
        grants=grants, jwt_exp=minted.expires_at, user_id=user_id
    )
