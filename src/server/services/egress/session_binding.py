"""Egress-relay binding for one workspace session: grants, JWT, credential file.

The sandbox's only relay credential is a short-lived JWT plus a server→grant
map, written to a single file (`upload_egress_relay_credentials`). This module
owns the lifecycle of that file across the resolve path (`sync_egress_relay`)
and the warm fast path (`maybe_remint_egress_jwt`). The file is the ONLY place
a grant id reaches the sandbox — resolved server configs carry none, so a
retired grant cannot survive in a second channel.

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
from typing import TYPE_CHECKING

from src.config.env import EGRESS_RELAY_SECRET
from src.server.database.egress_grants import sync_oauth_grants

if TYPE_CHECKING:
    from ptc_agent.core.session import Session
    from src.server.services.mcp_config import ResolvedMCP

logger = logging.getLogger(__name__)


async def sync_egress_relay(
    workspace_id: str,
    user_id: str | None,
    session: "Session",
    resolved: "ResolvedMCP",
) -> None:
    """Converge grants + relay JWT + sandbox credential file to ``resolved``.

    One grant per OAuth-connected server in the resolved set; grants the
    workspace no longer resolves are retired in the same transaction (they are
    an authorization overhang otherwise — the sandbox may still hold their ids
    and a live JWT). Removal of the last OAuth server also deletes the
    credential file, decided from the table so it converges on any worker.
    """
    oauth_servers = [s for s in resolved.servers if s.oauth_connection_id]
    if oauth_servers and not EGRESS_RELAY_SECRET:
        logger.warning(
            "[EGRESS] OAuth-connected MCP servers %s present but "
            "EGRESS_RELAY_SECRET is unset — they stay unbound",
            [s.name for s in oauth_servers],
        )
        return
    # OAuth connections only resolve for authenticated users, so an anonymous
    # turn can only ever be converging a removal.
    if oauth_servers and not user_id:
        return

    synced = await sync_oauth_grants(
        user_id=user_id or "",
        workspace_id=workspace_id,
        connection_ids=[s.oauth_connection_id for s in oauth_servers],
    )
    grants: dict[str, str] = {}
    for srv in oauth_servers:
        grant_id = synced.grants.get(srv.oauth_connection_id)
        if grant_id is None:
            # The connection vanished between resolve and here (disconnect
            # race): leave this one server unbound, keep binding the rest.
            logger.warning(
                "[EGRESS] connection %s gone for server %s — left unbound",
                srv.oauth_connection_id, srv.name,
            )
            continue
        grants[srv.name] = grant_id

    if grants or synced.retired or session.egress_binding is not None:
        await _push_credentials(workspace_id, session, user_id or "", grants)


async def maybe_remint_egress_jwt(workspace_id: str, session: "Session") -> None:
    """Re-push credentials when the relay JWT nears expiry.

    Runs on the warm-cooldown path (which skips the resolve entirely), so a
    long-lived session keeps a valid JWT without re-resolving config. Pure
    in-memory compare unless the token is actually near expiry.
    """
    binding = session.egress_binding
    if binding is None or not EGRESS_RELAY_SECRET:
        return
    from src.server.services.egress.relay_jwt import needs_remint

    if not needs_remint(binding.jwt_exp):
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
    """(Re)write the sandbox credential file; ``grants == {}`` deletes it.

    The binding records what the sandbox is known to hold, so it advances only
    on a publication the sandbox confirmed. No cross-worker lock is needed: the
    upload replaces the file atomically, so a concurrent push can at worst
    overwrite this one's file with an equally-valid credential — never tear it.
    """
    from ptc_agent.core.session import EgressBinding
    from src.server.services.egress.reachability import (
        effective_relay_base_url,
        relay_reachability_warning,
    )
    from src.server.services.egress.relay_jwt import mint_relay_jwt

    sandbox = session.sandbox
    if sandbox is None:
        return

    payload, binding = None, None
    if grants:
        provider = session.config.sandbox.provider
        relay_base = effective_relay_base_url(provider)
        warning = relay_reachability_warning(provider, relay_base)
        if warning:
            logger.warning("[EGRESS] %s", warning)
        minted = mint_relay_jwt(
            EGRESS_RELAY_SECRET,
            user_id=user_id,
            workspace_id=workspace_id,
            sandbox_id=sandbox.sandbox_id or "",
        )
        payload = {
            "relay_base_url": relay_base.rstrip("/"),
            "token": minted.token,
            "grants": grants,
        }
        binding = EgressBinding(
            grants=grants, jwt_exp=minted.expires_at, user_id=user_id
        )

    if not await sandbox.upload_egress_relay_credentials(payload):
        logger.warning(
            "[EGRESS] credential push failed for workspace %s — binding unchanged",
            workspace_id,
        )
        return
    session.egress_binding = binding
