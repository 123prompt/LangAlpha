"""What the automation PATCH path refuses, and what a pause and resume keep.

``create_automation`` refuses ``agent_mode='ptc'`` without a workspace. A PATCH
can flip the mode on its own, so the same requirement has to hold against the
merged state — otherwise the row is only rejected by the executor at run time,
which spends a failure against ``max_failures``. The same goes for a price
config the monitor cannot read, and for a trigger kind a PATCH cannot change.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from src.server.handlers.automation_handler import (
    pause_automation,
    resume_automation,
    update_automation,
)

OWNER = "user-owner"
AUTOMATION_ID = str(uuid.uuid4())
WORKSPACE_ID = str(uuid.uuid4())


def _row(**overrides):
    row = {
        "automation_id": AUTOMATION_ID,
        "user_id": OWNER,
        "trigger_type": "cron",
        "cron_expression": "0 9 * * 1-5",
        "timezone": "UTC",
        "status": "active",
        "agent_mode": "flash",
        "workspace_id": None,
    }
    row.update(overrides)
    return row


class TestPtcRequiresAWorkspace:
    @pytest.mark.asyncio
    @patch("src.server.handlers.automation_handler.auto_db")
    async def test_activating_ptc_without_any_workspace_is_rejected(self, mock_auto_db):
        mock_auto_db.get_automation = AsyncMock(return_value=_row())

        with pytest.raises(ValueError, match="workspace_id is required"):
            await update_automation(AUTOMATION_ID, OWNER, {"agent_mode": "ptc"})

        mock_auto_db.update_automation.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.server.handlers.automation_handler.auto_db")
    @patch("src.server.handlers.automation_handler.get_workspace")
    async def test_activating_ptc_with_a_workspace_in_the_same_patch_proceeds(
        self, mock_get_workspace, mock_auto_db,
    ):
        mock_auto_db.get_automation = AsyncMock(return_value=_row())
        mock_auto_db.update_automation = AsyncMock(return_value=_row(agent_mode="ptc"))
        mock_get_workspace.return_value = {
            "workspace_id": WORKSPACE_ID, "user_id": OWNER,
        }

        await update_automation(
            AUTOMATION_ID, OWNER,
            {"agent_mode": "ptc", "workspace_id": WORKSPACE_ID},
        )

        mock_auto_db.update_automation.assert_called_once()

    @pytest.mark.asyncio
    @patch("src.server.handlers.automation_handler.auto_db")
    @patch("src.server.handlers.automation_handler.get_workspace")
    async def test_activating_ptc_on_a_row_that_already_stores_one_proceeds(
        self, mock_get_workspace, mock_auto_db,
    ):
        """The stored workspace was ownership-checked when it was written, so
        the mode flip alone neither re-looks it up nor needs to."""
        mock_auto_db.get_automation = AsyncMock(
            return_value=_row(workspace_id=WORKSPACE_ID)
        )
        mock_auto_db.update_automation = AsyncMock(return_value=_row(agent_mode="ptc"))

        await update_automation(AUTOMATION_ID, OWNER, {"agent_mode": "ptc"})

        mock_get_workspace.assert_not_called()
        mock_auto_db.update_automation.assert_called_once()

    @pytest.mark.asyncio
    @patch("src.server.handlers.automation_handler.auto_db")
    async def test_leaving_ptc_for_flash_does_not_require_a_workspace(
        self, mock_auto_db,
    ):
        mock_auto_db.get_automation = AsyncMock(return_value=_row(agent_mode="ptc"))
        mock_auto_db.update_automation = AsyncMock(return_value=_row())

        await update_automation(AUTOMATION_ID, OWNER, {"agent_mode": "flash"})

        mock_auto_db.update_automation.assert_called_once()

    @pytest.mark.asyncio
    @patch("src.server.handlers.automation_handler.auto_db")
    async def test_unrelated_patch_on_a_ptc_row_is_not_falsely_rejected(
        self, mock_auto_db,
    ):
        mock_auto_db.get_automation = AsyncMock(
            return_value=_row(agent_mode="ptc", workspace_id=WORKSPACE_ID)
        )
        mock_auto_db.update_automation = AsyncMock(return_value=_row())

        await update_automation(AUTOMATION_ID, OWNER, {"name": "renamed"})

        mock_auto_db.update_automation.assert_called_once()


class TestTriggerKindIsFixed:
    @pytest.mark.asyncio
    @patch("src.server.handlers.automation_handler.auto_db")
    async def test_changing_the_kind_is_refused(self, mock_auto_db):
        mock_auto_db.get_automation = AsyncMock(return_value=_row())

        with pytest.raises(ValidationError, match="can't change from 'cron' to 'once'"):
            await update_automation(AUTOMATION_ID, OWNER, {"trigger_type": "once"})

        mock_auto_db.update_automation.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.server.handlers.automation_handler.auto_db")
    async def test_restating_the_same_kind_proceeds(self, mock_auto_db):
        mock_auto_db.get_automation = AsyncMock(return_value=_row())
        mock_auto_db.update_automation = AsyncMock(return_value=_row())

        await update_automation(
            AUTOMATION_ID, OWNER, {"trigger_type": "cron", "name": "renamed"}
        )

        mock_auto_db.update_automation.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("kind", "field", "value"),
        [
            ("price", "next_run_at", "2026-10-01T13:00:00Z"),
            ("once", "cron_expression", "0 9 * * *"),
            ("cron", "trigger_config", {
                "symbol": "AAPL", "conditions": [{"type": "price_above", "value": 200}],
            }),
        ],
    )
    @patch("src.server.handlers.automation_handler.auto_db")
    async def test_another_kinds_schedule_field_is_refused(self, mock_auto_db, kind, field, value):
        # A next_run_at stored on a price row would be claimed and fired by
        # the scheduler as if it were due.
        mock_auto_db.get_automation = AsyncMock(return_value=_row(trigger_type=kind))

        with pytest.raises(ValidationError, match=f"{field} doesn't apply to a '{kind}'"):
            await update_automation(AUTOMATION_ID, OWNER, {field: value})

        mock_auto_db.update_automation.assert_not_called()


PRICE_CONFIG = {"symbol": "AAPL", "conditions": [{"type": "price_above", "value": 200}]}


class TestPriceConfigOnUpdate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "config",
        [
            {**PRICE_CONFIG, "conditions": [{"type": "price_above", "value": 0}]},
            {**PRICE_CONFIG, "symbol": "^SPX"},
            {**PRICE_CONFIG, "symbol": "I:SPX"},
            {**PRICE_CONFIG, "symbol": "ABCDEFGHIJK"},
        ],
        ids=["zero-value", "caret", "I-prefix", "too-long"],
    )
    @patch("src.server.handlers.automation_handler.auto_db")
    async def test_a_config_the_monitor_would_skip_is_refused(self, mock_auto_db, config):
        mock_auto_db.get_automation = AsyncMock(
            return_value=_row(trigger_type="price", cron_expression=None)
        )

        with pytest.raises(ValueError, match="Invalid price trigger config"):
            await update_automation(AUTOMATION_ID, OWNER, {"trigger_config": config})

        mock_auto_db.update_automation.assert_not_called()


@pytest.mark.asyncio
@patch("src.server.handlers.automation_handler.auto_db")
async def test_an_empty_description_clears_it(mock_auto_db):
    mock_auto_db.get_automation = AsyncMock(return_value=_row(description="old"))
    mock_auto_db.update_automation = AsyncMock(return_value=_row(description=""))

    await update_automation(AUTOMATION_ID, OWNER, {"description": ""})

    assert mock_auto_db.update_automation.call_args.kwargs["description"] == ""


class TestPause:
    @pytest.mark.asyncio
    @patch("src.server.handlers.automation_handler.auto_db")
    async def test_a_one_time_automation_keeps_its_run_time(self, mock_auto_db):
        """Resume reads the time back; the scheduler claims only active rows."""
        mock_auto_db.get_automation = AsyncMock(
            return_value=_row(trigger_type="once", cron_expression=None)
        )
        mock_auto_db.update_automation = AsyncMock(return_value=_row(status="paused"))

        await pause_automation(AUTOMATION_ID, OWNER)

        mock_auto_db.update_automation.assert_awaited_once_with(
            AUTOMATION_ID, OWNER, status="paused"
        )

    @pytest.mark.asyncio
    @patch("src.server.handlers.automation_handler.auto_db")
    async def test_a_cron_clears_its_next_run(self, mock_auto_db):
        mock_auto_db.get_automation = AsyncMock(return_value=_row())
        mock_auto_db.update_automation = AsyncMock(return_value=_row(status="paused"))

        await pause_automation(AUTOMATION_ID, OWNER)

        mock_auto_db.update_automation.assert_awaited_once_with(
            AUTOMATION_ID, OWNER, status="paused", next_run_at=None
        )


class TestResumeOnce:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status, resumes", [("disabled", True), ("paused", False)])
    @patch("src.server.handlers.automation_handler.auto_db")
    async def test_a_past_time_resumes_only_what_the_server_switched_off(
        self, mock_auto_db, status, resumes
    ):
        """A switched-off one-time automation already had its run; back on, it
        waits for Run now. A paused one would silently never run."""
        mock_auto_db.get_automation = AsyncMock(
            return_value=_row(
                trigger_type="once", cron_expression=None, status=status,
                next_run_at=None,
            )
        )
        mock_auto_db.update_automation = AsyncMock(return_value=_row())

        if not resumes:
            with pytest.raises(ValueError, match="scheduled time has passed"):
                await resume_automation(AUTOMATION_ID, OWNER)
            return
        await resume_automation(AUTOMATION_ID, OWNER)
        mock_auto_db.update_automation.assert_awaited_once_with(
            AUTOMATION_ID, OWNER, status="active", failure_count=0,
            disable_reason=None, next_run_at=None,
        )


class TestCronEditRecomputesOnlyWhenActive:
    @pytest.mark.asyncio
    @patch("src.server.handlers.automation_handler.auto_db")
    async def test_active_cron_gets_a_new_next_run(self, mock_auto_db):
        mock_auto_db.get_automation = AsyncMock(return_value=_row())
        mock_auto_db.update_automation = AsyncMock(return_value=_row())

        await update_automation(AUTOMATION_ID, OWNER, {"cron_expression": "0 10 * * *"})

        assert mock_auto_db.update_automation.call_args.kwargs["next_run_at"] is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["paused", "disabled"])
    @patch("src.server.handlers.automation_handler.auto_db")
    async def test_an_edit_does_not_give_a_stopped_cron_a_next_run(self, mock_auto_db, status):
        """Resume computes it from the edited expression and zone."""
        mock_auto_db.get_automation = AsyncMock(
            return_value=_row(status=status, next_run_at=None)
        )
        mock_auto_db.update_automation = AsyncMock(return_value=_row(status=status))

        await update_automation(
            AUTOMATION_ID, OWNER, {"cron_expression": "0 10 * * *", "timezone": "Asia/Tokyo"}
        )

        assert "next_run_at" not in mock_auto_db.update_automation.call_args.kwargs


class TestThreadPin:
    @pytest.mark.asyncio
    @patch("src.server.handlers.automation_handler.auto_db")
    async def test_a_named_none_unpins_the_thread(self, mock_auto_db):
        """The agent tool's thread='new'. A pin left behind would be resumed
        by a later switch back to 'persistent'."""
        mock_auto_db.get_automation = AsyncMock(
            return_value=_row(
                thread_strategy="continue", conversation_thread_id=str(uuid.uuid4())
            )
        )
        mock_auto_db.update_automation = AsyncMock(return_value=_row())

        await update_automation(
            AUTOMATION_ID, OWNER, {"thread_strategy": "new", "conversation_thread_id": None}
        )

        written = mock_auto_db.update_automation.call_args.kwargs
        assert written["thread_strategy"] == "new"
        assert written["conversation_thread_id"] is None

    @pytest.mark.asyncio
    @patch("src.server.handlers.automation_handler.auto_db")
    async def test_an_update_that_names_no_thread_keeps_the_pin(self, mock_auto_db):
        mock_auto_db.get_automation = AsyncMock(return_value=_row())
        mock_auto_db.update_automation = AsyncMock(return_value=_row())

        await update_automation(AUTOMATION_ID, OWNER, {"name": "Renamed"})

        assert "conversation_thread_id" not in mock_auto_db.update_automation.call_args.kwargs
