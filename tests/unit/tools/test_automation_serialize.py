import uuid
from datetime import datetime, timezone

from src.tools.automation.tools import _serialize


def test_a_price_stays_a_number_and_an_id_becomes_a_string():
    automation_id = uuid.uuid4()
    serialized = _serialize(
        {
            "automation_id": automation_id,
            "trigger_config": {"conditions": [{"type": "price_above", "value": 150.5}]},
            "next_run_at": datetime(2026, 10, 28, 14, 15, tzinfo=timezone.utc),
        }
    )
    assert serialized == {
        "automation_id": str(automation_id),
        "trigger_config": {"conditions": [{"type": "price_above", "value": 150.5}]},
        "next_run_at": "2026-10-28T14:15:00+00:00",
    }
