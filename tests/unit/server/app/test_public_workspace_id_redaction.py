"""A shared replay never carries the owner's workspace id, however deep it sits."""

from __future__ import annotations

import pytest

from tests.unit.server.app import test_public_order_privacy as replay_suite

_replay = replay_suite._replay
client = replay_suite.client

pytestmark = pytest.mark.asyncio

_OWNER_WS = "0f1e2d3c-4b5a-4968-8776-655443322110"

_CHART_EVENTS = [
    {
        "event": "artifact",
        "data": {
            "artifact_type": "chart_annotation",
            "artifact_id": "ann-1",
            "payload": {
                "op": "add",
                "workspace_id": _OWNER_WS,
                "symbol": "NVDA",
                "annotations": [{"id": "ann-1", "workspace_id": _OWNER_WS}],
            },
        },
    },
    {
        "event": "tool_call_result",
        "data": {
            "tool_call_id": "call_chart",
            "content": "Added a support line.",
            "artifact": {
                "type": "chart_annotation",
                "symbol": "NVDA",
                "workspace_id": _OWNER_WS,
            },
        },
    },
]


async def test_replay_strips_workspace_ids_nested_in_artifacts(client):
    body, events = await _replay(client, _CHART_EVENTS)
    assert _OWNER_WS not in body
    artifact = next(e for e in events if e["event"] == "artifact")["data"]
    # The card still has what it draws from.
    assert artifact["payload"]["symbol"] == "NVDA"
    assert artifact["payload"]["annotations"] == [{"id": "ann-1"}]
    result = next(e for e in events if e["event"] == "tool_call_result")["data"]
    assert result["artifact"] == {"type": "chart_annotation", "symbol": "NVDA"}


async def test_replay_strips_workspace_ids_nested_in_query_metadata(client):
    context = {
        "type": "widget",
        "widget_type": "markets.chart",
        "data": {"workspace_id": _OWNER_WS, "symbol": "NVDA"},
    }
    body, events = await _replay(
        client,
        _CHART_EVENTS,
        later_events=[],
        later_metadata={"additional_context": [context]},
    )
    assert _OWNER_WS not in body
    later = [e for e in events if e["event"] == "user_message"][1]["data"]["metadata"]
    assert later["additional_context"][0]["data"] == {"symbol": "NVDA"}
