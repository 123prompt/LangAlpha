"""The whole-turn budget over directly bound MCP tools.

Per-server discovery caps size the cached JSON; these size what one model
request is asked to carry, which nothing bounded before.
"""

from dataclasses import dataclass

import pytest

from src.server.services.egress.direct_tools import (
    MAX_DIRECT_SCHEMA_CHARS,
    MAX_DIRECT_TOOLS,
    admit_within_budget,
)
from src.server.services.egress import direct_tools


@dataclass
class _Entry:
    schemas: tuple


def _schemas(prefix: str, n: int, description: str = "d") -> tuple:
    return tuple(
        {"name": f"{prefix}_{i}", "description": description, "input_schema": {}}
        for i in range(n)
    )


def test_everything_fits_when_the_set_is_small():
    by_server = {"a": _Entry(_schemas("a", 3)), "b": _Entry(_schemas("b", 2))}
    admitted, dropped = admit_within_budget(by_server)
    assert dropped == []
    assert [s["name"] for s in admitted["a"]] == ["a_0", "a_1", "a_2"]
    assert [s["name"] for s in admitted["b"]] == ["b_0", "b_1"]


def test_a_crowded_server_cannot_starve_a_small_one():
    by_server = {"big": _Entry(_schemas("big", 200)), "small": _Entry(_schemas("small", 4))}
    admitted, dropped = admit_within_budget(by_server)
    # Round-robin: the small server is reached on the first four rotations, so
    # it keeps every tool even though the big one alone overruns the cap.
    assert [s["name"] for s in admitted["small"]] == ["small_0", "small_1", "small_2", "small_3"]
    assert len(admitted["big"]) == MAX_DIRECT_TOOLS - 4
    assert dropped and all(server == "big" for server, _ in dropped)


def test_the_tool_count_cap_is_the_total_not_the_per_server_one():
    by_server = {name: _Entry(_schemas(name, 40)) for name in ("a", "b", "c")}
    admitted, _ = admit_within_budget(by_server)
    assert sum(len(v) for v in admitted.values()) == MAX_DIRECT_TOOLS


def test_a_fat_schema_is_charged_by_size_not_by_count():
    fat = "x" * (MAX_DIRECT_SCHEMA_CHARS // 4)
    by_server = {"a": _Entry(_schemas("a", 8, description=fat))}
    admitted, dropped = admit_within_budget(by_server)
    assert len(admitted["a"]) < 8
    assert dropped
    # Well under the count cap, so size is what stopped it.
    assert len(admitted["a"]) < MAX_DIRECT_TOOLS


def test_a_server_with_no_schemas_is_not_an_error():
    by_server = {"a": _Entry(()), "b": _Entry(_schemas("b", 2))}
    admitted, dropped = admit_within_budget(by_server)
    assert admitted["a"] == []
    assert len(admitted["b"]) == 2
    assert dropped == []


@pytest.mark.asyncio
async def test_a_server_without_a_grant_does_not_spend_the_budget(monkeypatch):
    """A connection can go away between the split and the bind.

    Its schemas are still in ``by_server`` but it has no grant, so it is
    skipped moments later. Charging it first would displace tools from a
    healthy connection and leave the capacity spent on nothing.
    """
    monkeypatch.setattr(direct_tools, "EGRESS_RELAY_SECRET", "s")
    seen: dict = {}

    def _spy(by_server):
        seen["names"] = sorted(by_server)
        return {name: [] for name in by_server}, []

    monkeypatch.setattr(direct_tools, "admit_within_budget", _spy)

    await direct_tools.prepare_direct_mcp_tools(
        user_id="u",
        workspace_id="w",
        sandbox_id="sb",
        grants={"live": "grant-1"},
        by_server={"live": _Entry(_schemas("live", 2)), "dead": _Entry(_schemas("dead", 99))},
    )

    assert seen["names"] == ["live"]
