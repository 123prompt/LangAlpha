"""Directly bound MCP tools: JSON tool calls from the model, through the relay.

The sandbox path hands the model a Python wrapper per MCP tool and lets it
compose them in ``ExecuteCode``. The direct path binds the tool itself, with
its vendor schema verbatim, so a call is one typed tool call the middleware
sees and the UI can render. Both paths reach the vendor through the same
egress grant and the same relay route: the host dials its own relay on
loopback with a relay JWT, so policy, token attach and audit are one code
path however the call arrived.

The relay sessions are opened inside the run, not the request. The graph is
driven by the run manager's task and outlives the request's generator when
the client drops, and the transport's session lives in the task that entered
it, so the sessions are entered and closed by ``drive`` around the run
stream itself. Between them the client is held open, so each call reuses
one session instead of paying a handshake through the relay.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import warnings
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from hashlib import sha1
from typing import TYPE_CHECKING, Any, Mapping

from langchain_core.tools import BaseTool

from ptc_agent.agent.middleware.direct_mcp import METADATA_KEY
from src.config.env import EGRESS_RELAY_LOOPBACK_URL, EGRESS_RELAY_SECRET
from src.server.database.mcp_oauth import SERVABLE, get_connection
from src.server.services.brokerage_capabilities import denied_tools, vendor_for_url
from src.server.services.egress import folded_contains
from src.server.services.egress.relay_jwt import CALLER_HOST, mint_relay_jwt
from src.server.services.mcp_tool_split import DirectServerTools

if TYPE_CHECKING:
    from ptc_agent.core.session import Session

logger = logging.getLogger(__name__)

_NAME_SAFE = re.compile(r"[^A-Za-z0-9_-]")
# The strictest provider limit on a tool name.
_MAX_TOOL_NAME = 64

# The sandbox_id claim on a host-only turn. Audit-only at the relay, which
# authorizes on the (user, workspace) pair, but the claim must be non-empty.
FLASH_SANDBOX_ID = "flash"


def direct_tool_name(server: str, tool: str) -> str:
    """``mcp__<server>__<tool>``: distinct per server so two vendors' ``account_*``
    tools cannot collide, and prefixed so the frontend can route on it.

    The plain form is kept only while it is unambiguous. A segment the provider
    would reject, a server name carrying the ``__`` separator, or an overflow of
    the limit each fall back to a digest of the *pair*, because two tools bound
    under one name are not merely mislabelled: the tools node keeps whichever
    came last, and ``DirectMcpPolicyMiddleware`` then gates the call against the
    other tool's connection.

    Over the limit it is the *server* that gives way, never the tool: a long
    server name would otherwise eat the whole budget and leave ``read_order``
    and ``cancel_order`` the same name. Only a tool long enough to overrun on
    its own is trimmed, and the digest sits ahead of the cut so identity
    survives it.
    """
    safe_server = _NAME_SAFE.sub("_", server).replace("__", "_")
    safe_tool = _NAME_SAFE.sub("_", tool)
    plain = f"mcp__{safe_server}__{safe_tool}"
    if plain == f"mcp__{server}__{tool}" and len(plain) <= _MAX_TOOL_NAME:
        return plain
    digest = sha1(f"{server}\x00{tool}".encode()).hexdigest()[:8]
    tail = f"_{digest}__{safe_tool}"
    keep = _MAX_TOOL_NAME - len("mcp__") - len(tail)
    if keep >= 1:
        return f"mcp__{safe_server[:keep]}{tail}"
    return f"mcp__{safe_server[:1]}_{digest}__{safe_tool}"[:_MAX_TOOL_NAME]


# A turn's whole direct budget, across every server it binds. The per-server
# discovery caps sit at 128 tools and 400,000 schema characters, but those
# size the cached JSON and the sandbox wrapper module; nothing there bounds
# what a provider is asked to accept in one request. These do. The agent
# already carries roughly 25 to 40 tools of its own, so 64 direct ones keep a
# turn near a hundred definitions, and 120,000 characters of schema is about
# thirty thousand tokens, which is a large but survivable slice of context.
MAX_DIRECT_TOOLS = 64
MAX_DIRECT_SCHEMA_CHARS = 120_000


def admit_within_budget(
    by_server: Mapping[str, Any],
) -> tuple[dict[str, list[dict]], list[tuple[str, str]]]:
    """Split each server's schemas into the ones a turn can afford, and the rest.

    Taken one per server in rotation rather than server by server, so a broker
    publishing eighty tools cannot spend the whole budget before a second
    connection is reached at all: every server keeps a usable share, and a user
    who wants more of one narrows the others on the Plugins page.
    """
    queues = {name: list(entry.schemas or ()) for name, entry in by_server.items()}
    admitted: dict[str, list[dict]] = {name: [] for name in queues}
    dropped: list[tuple[str, str]] = []
    tools = 0
    chars = 0
    while any(queues.values()):
        for name, queue in queues.items():
            if not queue:
                continue
            schema = queue.pop(0)
            if tools >= MAX_DIRECT_TOOLS:
                dropped.append((name, str(schema.get("name") or "")))
                continue
            size = len(json.dumps(schema, ensure_ascii=False, default=str))
            if chars + size > MAX_DIRECT_SCHEMA_CHARS:
                dropped.append((name, str(schema.get("name") or "")))
                continue
            admitted[name].append(schema)
            tools += 1
            chars += size
    return admitted, dropped


# How long a turn waits for one relay session before giving up on opening it
# ahead of time. Opening early only buys the first call's handshake, so a
# session that is not ready quickly is worth less than the delay it adds in
# front of the first token. A server that times out here is treated exactly
# like one that refused to open: warned about, left closed, and reopened per
# call by the tools that need it.
SESSION_OPEN_TIMEOUT_S = 5.0


@dataclass
class DirectMCPBinding:
    """The tools bound for one turn, the clients behind them, and the policy.

    ``check`` is the port ``DirectMcpPolicyMiddleware`` calls per tool call. It
    reads the connection row again rather than the set bound at turn start:
    consent withdrawn on the Plugins page mid-turn, or a connection that went
    to ``needs_reauth``, refuses the next call instead of the next turn.
    """

    user_id: str | None = None
    tools: list[BaseTool] = field(default_factory=list)
    _clients: list[tuple[str, Any]] = field(default_factory=list)

    def add_server(self, name: str, client: Any) -> None:
        self._clients.append((name, client))

    async def check(self, server: str, tool: str) -> str | None:
        if not self.user_id:
            return "no user is attached to this turn"
        connection = await get_connection(self.user_id, server)
        if connection is None:
            return f"{server} is no longer connected"
        if connection.status not in SERVABLE:
            return f"{server} needs to be reconnected before it can be used"
        vendor = vendor_for_url(connection.server_url)
        denied = denied_tools(vendor, connection.granted_capabilities or ())
        if denied and folded_contains(denied, tool):
            return f"the connection to {server} does not permit {tool}"
        return None

    async def drive(self, stream: AsyncIterator[Any]) -> AsyncIterator[Any]:
        """Run ``stream`` with every relay session open around it.

        A server whose session cannot be opened is left closed with a warning
        rather than failing the turn: its tools then open a session per call,
        or fail per call with the same reason, and the rest of the toolset is
        unaffected.
        """
        if not self._clients:
            async for event in stream:
                yield event
            return
        async with AsyncExitStack() as stack:

            async def _open(server: str, client: Any) -> None:
                try:
                    await asyncio.wait_for(
                        stack.enter_async_context(client), SESSION_OPEN_TIMEOUT_S
                    )
                except TimeoutError:
                    logger.warning(
                        "[DIRECT_MCP] %r: relay session did not open within %ss; "
                        "its tools will open one per call",
                        server,
                        SESSION_OPEN_TIMEOUT_S,
                    )
                except Exception as e:
                    logger.warning(
                        "[DIRECT_MCP] %r: relay session failed to open: %s", server, e
                    )

            # Concurrently, because these handshakes stand between the user and
            # the first token: opened in sequence, one slow server delays every
            # server behind it and the waits add up across the whole set, so a
            # turn that would never call those tools still looks hung.
            await asyncio.gather(*(_open(s, c) for s, c in self._clients))
            async for event in stream:
                yield event


async def prepare_direct_mcp_tools(
    *,
    user_id: str | None,
    workspace_id: str,
    sandbox_id: str,
    grants: Mapping[str, str],
    by_server: Mapping[str, DirectServerTools],
) -> DirectMCPBinding:
    """Build the tools for one turn; nothing is dialled until ``drive``."""
    binding = DirectMCPBinding(user_id=user_id)
    if not by_server or not grants or not EGRESS_RELAY_SECRET or not user_id:
        return binding

    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from mcp.types import Tool

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from langchain.mcp import as_langchain_tool

    minted = mint_relay_jwt(
        EGRESS_RELAY_SECRET,
        user_id=user_id,
        workspace_id=workspace_id,
        sandbox_id=sandbox_id,
        caller=CALLER_HOST,
    )
    base = EGRESS_RELAY_LOOPBACK_URL.rstrip("/")

    # Only a granted server can be reached, so only a granted server may spend
    # the budget: one that lost its grant would otherwise displace tools from a
    # healthy connection and then be skipped anyway, leaving the capacity spent
    # on nothing.
    grantable = {name: entry for name, entry in by_server.items() if grants.get(name)}
    affordable, dropped = admit_within_budget(grantable)
    if dropped:
        logger.warning(
            "[DIRECT_MCP] over the turn budget of %d tools / %d schema chars: "
            "dropped %d tool(s): %s",
            MAX_DIRECT_TOOLS,
            MAX_DIRECT_SCHEMA_CHARS,
            len(dropped),
            ", ".join(f"{s}/{t}" for s, t in dropped[:20]),
        )

    for server, entry in by_server.items():
        grant_id = grants.get(server)
        schemas = affordable.get(server) or ()
        if not grant_id or not schemas:
            continue
        client = Client(
            StreamableHttpTransport(
                url=f"{base}/v1/egress/{grant_id}",
                headers={"Authorization": f"Bearer {minted.token}"},
            )
        )
        bound = 0
        for schema in schemas:
            try:
                tool = Tool(
                    name=schema["name"],
                    description=schema.get("description"),
                    input_schema=schema.get("input_schema") or {"type": "object"},
                )
                # No I/O for a client that is not entered: the session opens
                # in ``drive``.
                lc_tool = await as_langchain_tool(tool, client)
            except Exception as e:
                logger.warning(
                    "[DIRECT_MCP] %r/%r: could not bind: %s",
                    server,
                    schema.get("name"),
                    e,
                )
                continue
            lc_tool.name = direct_tool_name(server, tool.name)
            lc_tool.metadata = {
                **(lc_tool.metadata or {}),
                METADATA_KEY: {
                    "server": server,
                    "tool": tool.name,
                    # A `both` tool keeps its sandbox wrapper, so the prompt
                    # must not tell the model this one is call-only.
                    "sandboxed": not folded_contains(
                        entry.sandbox_excluded, tool.name
                    ),
                },
            }
            binding.tools.append(lc_tool)
            bound += 1
        if bound:
            binding.add_server(server, client)
            logger.info(
                "[DIRECT_MCP] %r: bound %d tool(s) through grant %s",
                server,
                bound,
                grant_id,
            )
    return binding


async def bind_direct_mcp_tools(
    session: "Session", *, user_id: str | None
) -> DirectMCPBinding:
    """The PTC shape: the session carries the split and the grants."""
    by_server = session.direct_mcp_tools
    egress = session.egress_binding
    if not by_server or egress is None:
        return DirectMCPBinding(user_id=user_id)
    return await prepare_direct_mcp_tools(
        user_id=user_id,
        workspace_id=session.conversation_id,
        sandbox_id=getattr(session.sandbox, "sandbox_id", None) or "",
        grants=egress.grants,
        by_server=by_server,
    )
