"""The shared replay has to carry the turn's end, or its fold row has no duration.

``build_sse_replay_items`` is not on this path: ``public.py`` hand-builds its own
``user_message`` payload so it can drop the keys a viewer must not read. That
makes it a second construction site for the same event, and the completion stamp
the fold row pairs with the query timestamp has to be mirrored into it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_test_app

pytestmark = pytest.mark.asyncio

_SHARE_TOKEN = "share_abc123"
_THREAD_ID = "44444444-4444-4444-8444-444444444444"

_THREAD_BY_TOKEN = "src.server.app.share_access.get_thread_by_share_token"
_QUERIES = "src.server.app.public.get_queries_for_thread"
_RESPONSES = "src.server.app.public.get_responses_for_thread"
_TASK_DETAILS = "src.server.services.history.task_status.resolve_task_details"

_STARTED = datetime(2026, 9, 21, 15, 4, 0, tzinfo=timezone.utc)
_SETTLED = _STARTED + timedelta(seconds=78)


@pytest_asyncio.fixture
async def client():
    from src.server.app.public import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _replay(client, response_row: dict) -> list[dict]:
    thread = {
        "conversation_thread_id": _THREAD_ID,
        "workspace_id": "ws-1",
        "share_permissions": {},
    }
    queries = [
        {
            "turn_index": 0,
            "content": "how long did that take",
            "created_at": _STARTED,
            "metadata": {},
        }
    ]
    with (
        patch(_THREAD_BY_TOKEN, new=AsyncMock(return_value=thread)),
        patch(_QUERIES, new=AsyncMock(return_value=(queries, None))),
        patch(_RESPONSES, new=AsyncMock(return_value=([response_row], None))),
        patch(_TASK_DETAILS, new=AsyncMock(return_value={})),
    ):
        resp = await client.get(f"/api/v1/public/shared/{_SHARE_TOKEN}/replay")
        assert resp.status_code == 200
        body = resp.text

    events = []
    for block in body.split("\n\n"):
        kind = payload = None
        for line in block.splitlines():
            if line.startswith("event: "):
                kind = line[len("event: ") :]
            elif line.startswith("data: "):
                payload = json.loads(line[len("data: ") :])
        if kind:
            events.append({"event": kind, "data": payload})
    return events


def _row(**overrides) -> dict:
    row = {
        "turn_index": 0,
        "conversation_response_id": "55555555-5555-4555-8555-555555555555",
        "status": "completed",
        "usage_settled_at": _SETTLED,
        "sse_events": [{"event": "message", "data": {"content": "done"}}],
    }
    row.update(overrides)
    return row


def _user_message(events: list[dict]) -> dict:
    return next(e for e in events if e["event"] == "user_message")["data"]


def test_the_public_query_actually_selects_the_settle_instant():
    """The mock above can hand the route any shape; production cannot.

    The owner's replay reads ``_SETTLED_ATTEMPTS`` directly and gets the column
    from its ``*``. The public route goes through ``get_responses_for_thread``,
    which projects ``_RESPONSE_COLUMNS``, so the preferred branch is only
    reachable while that list carries the column.
    """
    from src.server.database.conversation import _sql

    assert "usage_settled_at" in _sql._RESPONSE_COLUMNS


async def test_settled_turn_carries_its_end(client):
    payload = _user_message(await _replay(client, _row()))
    assert payload["run_completed_at"] == _SETTLED.isoformat()


async def test_falls_back_to_start_plus_duration(client):
    row = _row(usage_settled_at=None, created_at=_STARTED, execution_time=78.0)
    payload = _user_message(await _replay(client, row))
    assert payload["run_completed_at"] == _SETTLED.isoformat()


async def test_running_turn_carries_no_end(client):
    payload = _user_message(await _replay(client, _row(status="running")))
    assert "run_completed_at" not in payload


async def test_run_id_stays_off_the_public_stream(client):
    # The owner's replay stamps it so the report-back catch-up can skip a turn
    # already on screen. A public viewer runs no catch-up, so the identifier has
    # no reason to leave the account.
    payload = _user_message(await _replay(client, _row()))
    assert "run_id" not in payload


async def test_reasoning_duration_survives_the_public_copy(client):
    # The client reads `elapsed_ms` off the stored close to label the row. The
    # stored events are copied verbatim here, so this is the wire half of that
    # contract rather than a transformation.
    row = _row(
        sse_events=[
            {
                "event": "message_chunk",
                "data": {
                    "content_type": "reasoning_signal",
                    "content": "complete",
                    "elapsed_ms": 23000,
                },
            }
        ]
    )
    chunk = next(
        e for e in await _replay(client, row) if e["event"] == "message_chunk"
    )
    assert chunk["data"]["elapsed_ms"] == 23000
