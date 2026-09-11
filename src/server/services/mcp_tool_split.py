"""Turn one server's discovered tools into what each path gets.

The composite install and the Flash binder both start from the same three
inputs, the vendor's list, the consent's denial and the row's binding plan,
and must land on the same split, or a tool could be wrapped for the sandbox
on one path and bound directly on the other. Folded names throughout, the way
the relay matches them, so a vendor that recases a name is treated the same
at every gate.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from src.server.services.brokerage_capabilities import vendor_for_url
from src.server.services.brokerage_tool_overlays import overlay_tool_schemas
from src.server.services.egress import folded_contains
from src.server.services.tool_binding import BindingPlan


@dataclass(frozen=True)
class DirectServerTools:
    """One server's directly bound tools, as the vendor publishes their schemas.

    ``sandbox_excluded`` is carried alongside because ``both`` and ``direct``
    are indistinguishable in ``schemas`` alone, and the prompt has to tell the
    model which of these it may still import: a ``both`` tool that reads as
    direct-only loses the Python path it was bound ``both`` to keep.
    """

    schemas: tuple[dict, ...]
    sandbox_excluded: frozenset[str] = frozenset()


def split_server_tools(
    tools: list[dict],
    *,
    denied: frozenset[str] | None,
    plan: BindingPlan | None,
) -> tuple[list[dict], DirectServerTools | None]:
    """``(sandbox_tools, direct)``.

    ``sandbox_tools`` is what the wrappers and docs are generated from;
    ``direct`` is None when nothing on this server is bound directly.
    """
    if denied:
        tools = [t for t in tools if not folded_contains(denied, t.get("name"))]
    if plan is None or not plan.direct:
        return tools, None
    direct_schemas = tuple(
        t for t in tools if folded_contains(plan.direct, t.get("name"))
    )
    direct = (
        DirectServerTools(
            schemas=direct_schemas, sandbox_excluded=plan.sandbox_excluded
        )
        if direct_schemas
        else None
    )
    sandbox_tools = [
        t for t in tools if not folded_contains(plan.sandbox_excluded, t.get("name"))
    ]
    return sandbox_tools, direct


def build_direct_entries(
    servers: Iterable[Any],
    snapshots: Any,
    *,
    denied: Mapping[str, frozenset[str]],
    plans: Mapping[str, BindingPlan],
) -> tuple[dict[str, list[dict]], dict[str, DirectServerTools]]:
    """Split every server with a current snapshot, keyed by server name.

    A server with no usable snapshot appears in neither result, which is also
    how the composite install reads settlement.
    """
    sandbox_by_server: dict[str, list[dict]] = {}
    direct_by_server: dict[str, DirectServerTools] = {}
    for server in servers:
        snapshot = snapshots.ok(server)
        if snapshot is None:
            continue
        tools = overlay_tool_schemas(
            vendor_for_url(server.url), snapshot.get("tools") or []
        )
        sandbox_tools, direct = split_server_tools(
            tools, denied=denied.get(server.name), plan=plans.get(server.name)
        )
        sandbox_by_server[server.name] = sandbox_tools
        if direct is not None:
            direct_by_server[server.name] = direct
    return sandbox_by_server, direct_by_server
