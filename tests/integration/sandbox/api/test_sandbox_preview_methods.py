"""Integration tests for the sandbox's preview server and background command methods.

A real PTCSandbox on MemoryProvider: start/stop/logs for preview servers and
background commands. The legacy ``/api/v1/preview`` redirect never touches a
sandbox, so its tests are unit tests (``tests/unit/core/sandbox/test_preview.py``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from ptc_agent.core.sandbox.runtime import SessionCommandResult
from tests.integration.sandbox.conftest import _make_core_config
from tests.integration.sandbox.memory_provider import MemoryProvider

from .conftest import TEST_PROJECT, _make_workspace

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

def _workspace_for(sandbox, *, status="running"):
    return _make_workspace(
        status=status,
        computer_root_dir=sandbox.config.filesystem.working_directory,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sandbox_base_dir(tmp_path):
    d = tmp_path / "sandboxes"
    d.mkdir()
    return str(d)


@pytest_asyncio.fixture
async def sandbox(sandbox_base_dir):
    """Self-contained PTCSandbox backed by MemoryProvider."""
    from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

    provider = MemoryProvider(base_dir=sandbox_base_dir)
    config = _make_core_config(working_directory=sandbox_base_dir)
    with patch(
        "ptc_agent.core.sandbox.ptc_sandbox.create_provider",
        return_value=provider,
    ):
        sb = PTCSandbox(config)
        await sb.setup_sandbox_workspace(dir_name=TEST_PROJECT.dir_name)
        actual_work_dir = await sb.runtime.fetch_working_dir()
        sb.config.filesystem.working_directory = actual_work_dir
        sb.config.filesystem.allowed_directories = [actual_work_dir, "/tmp"]
        yield sb
        try:
            await sb.cleanup()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Sandbox method tests: Preview Server lifecycle
# ---------------------------------------------------------------------------


class TestStartPreviewServer:
    """PTCSandbox.start_preview_server with MemoryProvider.

    MemoryProvider's runtime does not implement sessions (raises
    NotImplementedError), so we mock the session methods on the runtime
    to verify the sandbox-level orchestration logic.
    """

    async def test_creates_per_port_session(self, sandbox):
        """Each start owns a distinct session so another worker cannot replace it."""
        created_sessions = []

        async def fake_create_session(session_id):
            created_sessions.append(session_id)

        async def fake_session_execute(session_id, command, *, run_async=False, timeout=None):
            return SessionCommandResult(
                cmd_id="cmd-001", exit_code=None, stdout="", stderr="",
            )

        sandbox.runtime.create_session = fake_create_session
        sandbox.runtime.session_execute = fake_session_execute

        cmd_id = await sandbox.start_preview_server("python -m http.server 8080", 8080)

        assert cmd_id == "cmd-001"
        assert any(sid.startswith("preview-8080-") for sid in created_sessions)
        assert 8080 in sandbox._preview_sessions
        session_id, stored_cmd_id = sandbox._preview_sessions[8080]
        assert session_id in created_sessions
        assert session_id.startswith("preview-8080-")
        assert stored_cmd_id == "cmd-001"

    async def test_replaces_existing_session_on_same_port(self, sandbox):
        """Starting on a port that already has a session tears down the old one."""
        deleted_sessions = []
        created_sessions = []
        call_count = 0

        async def fake_create_session(session_id):
            created_sessions.append(session_id)

        async def fake_delete_session(session_id):
            deleted_sessions.append(session_id)

        async def fake_session_execute(session_id, command, *, run_async=False, timeout=None):
            nonlocal call_count
            call_count += 1
            return SessionCommandResult(
                cmd_id=f"cmd-{call_count:03d}",
                exit_code=None,
                stdout="",
                stderr="",
            )

        sandbox.runtime.create_session = fake_create_session
        sandbox.runtime.delete_session = fake_delete_session
        sandbox.runtime.session_execute = fake_session_execute

        # First start
        cmd_id_1 = await sandbox.start_preview_server("python -m http.server 8080", 8080)
        assert cmd_id_1 == "cmd-001"
        assert 8080 in sandbox._preview_sessions

        first_session = sandbox._preview_sessions[8080][0]
        # Second start on the same port
        cmd_id_2 = await sandbox.start_preview_server("python -m http.server 8080", 8080)
        assert cmd_id_2 == "cmd-002"

        # Old session should have been deleted, and the replacement is distinct.
        assert first_session in deleted_sessions
        assert sandbox._preview_sessions[8080][0] != first_session
        # New session entry should replace the old one
        _, stored_cmd_id = sandbox._preview_sessions[8080]
        assert stored_cmd_id == "cmd-002"

    async def test_multiple_ports_get_separate_sessions(self, sandbox):
        """Different ports get different sessions."""
        created_sessions = []
        call_count = 0

        async def fake_create_session(session_id):
            created_sessions.append(session_id)

        async def fake_session_execute(session_id, command, *, run_async=False, timeout=None):
            nonlocal call_count
            call_count += 1
            return SessionCommandResult(
                cmd_id=f"cmd-{call_count:03d}",
                exit_code=None,
                stdout="",
                stderr="",
            )

        sandbox.runtime.create_session = fake_create_session
        sandbox.runtime.session_execute = fake_session_execute

        await sandbox.start_preview_server("python -m http.server 3000", 3000)
        await sandbox.start_preview_server("python -m http.server 8080", 8080)

        assert any(sid.startswith("preview-3000-") for sid in created_sessions)
        assert any(sid.startswith("preview-8080-") for sid in created_sessions)
        assert 3000 in sandbox._preview_sessions
        assert 8080 in sandbox._preview_sessions
        assert sandbox._preview_sessions[3000][0] in created_sessions
        assert sandbox._preview_sessions[8080][0] in created_sessions
        assert sandbox._preview_sessions[3000][0] != sandbox._preview_sessions[8080][0]


class TestStopPreviewServer:
    """PTCSandbox.stop_preview_server with mocked runtime sessions."""

    async def test_stop_deletes_session_and_cleans_up(self, sandbox):
        """Stopping a preview server deletes the session and removes tracking."""
        deleted_sessions = []

        async def fake_create_session(session_id):
            pass

        async def fake_delete_session(session_id):
            deleted_sessions.append(session_id)

        async def fake_session_execute(session_id, command, *, run_async=False, timeout=None):
            return SessionCommandResult(
                cmd_id="cmd-001", exit_code=None, stdout="", stderr="",
            )

        sandbox.runtime.create_session = fake_create_session
        sandbox.runtime.delete_session = fake_delete_session
        sandbox.runtime.session_execute = fake_session_execute

        await sandbox.start_preview_server("python -m http.server 8080", 8080)
        assert 8080 in sandbox._preview_sessions

        session_id = sandbox._preview_sessions[8080][0]
        result = await sandbox.stop_preview_server(8080)

        assert result is True
        assert session_id in deleted_sessions
        assert 8080 not in sandbox._preview_sessions

    async def test_stop_nonexistent_port_returns_false(self, sandbox):
        """Stopping a port with no preview server returns False."""
        result = await sandbox.stop_preview_server(9999)
        assert result is False


class TestGetPreviewServerLogs:
    """PTCSandbox.get_preview_server_logs with mocked runtime sessions."""

    async def test_logs_for_running_server(self, sandbox):
        """Get logs for a running preview server."""
        async def fake_create_session(session_id):
            pass

        async def fake_session_execute(session_id, command, *, run_async=False, timeout=None):
            return SessionCommandResult(
                cmd_id="cmd-001", exit_code=None, stdout="", stderr="",
            )

        async def fake_session_command_logs(session_id, cmd_id):
            return SessionCommandResult(
                cmd_id=cmd_id,
                exit_code=None,  # still running
                stdout="Serving HTTP on 0.0.0.0 port 8080\n",
                stderr="",
            )

        sandbox.runtime.create_session = fake_create_session
        sandbox.runtime.session_execute = fake_session_execute
        sandbox.runtime.session_command_logs = fake_session_command_logs

        await sandbox.start_preview_server("python -m http.server 8080", 8080)

        logs = await sandbox.get_preview_server_logs(8080)

        assert logs["success"] is True
        assert logs["is_running"] is True
        assert logs["port"] == 8080
        assert "Serving HTTP" in logs["stdout"]

    async def test_logs_for_nonexistent_port(self, sandbox):
        """Getting logs for a non-tracked port returns a failure result."""
        logs = await sandbox.get_preview_server_logs(9999)

        assert logs["success"] is False
        assert logs["is_running"] is False
        assert logs["port"] == 9999
        assert "No preview session" in logs["stderr"]

    async def test_logs_for_exited_server(self, sandbox):
        """Logs show exit code when server has stopped."""
        async def fake_create_session(session_id):
            pass

        async def fake_session_execute(session_id, command, *, run_async=False, timeout=None):
            return SessionCommandResult(
                cmd_id="cmd-001", exit_code=None, stdout="", stderr="",
            )

        async def fake_session_command_logs(session_id, cmd_id):
            return SessionCommandResult(
                cmd_id=cmd_id,
                exit_code=1,
                stdout="",
                stderr="Address already in use",
            )

        sandbox.runtime.create_session = fake_create_session
        sandbox.runtime.session_execute = fake_session_execute
        sandbox.runtime.session_command_logs = fake_session_command_logs

        await sandbox.start_preview_server("python -m http.server 8080", 8080)

        logs = await sandbox.get_preview_server_logs(8080)

        assert logs["success"] is True
        assert logs["is_running"] is False
        assert logs["exit_code"] == 1
        assert "Address already in use" in logs["stderr"]


# ---------------------------------------------------------------------------
# Sandbox method tests: Background command lifecycle
# ---------------------------------------------------------------------------


class TestBackgroundCommandStop:
    """PTCSandbox.stop_background_command via execute_bash_command(background=True)."""

    async def test_run_and_stop_background_command(self, sandbox):
        """Start a background command, then stop it."""
        created_sessions = []
        deleted_sessions = []
        call_count = 0

        async def fake_create_session(session_id):
            created_sessions.append(session_id)

        async def fake_delete_session(session_id):
            deleted_sessions.append(session_id)

        async def fake_session_execute(session_id, command, *, run_async=False, timeout=None):
            nonlocal call_count
            call_count += 1
            return SessionCommandResult(
                cmd_id=f"bg-cmd-{call_count:03d}",
                exit_code=None,
                stdout="",
                stderr="",
            )

        sandbox.runtime.create_session = fake_create_session
        sandbox.runtime.delete_session = fake_delete_session
        sandbox.runtime.session_execute = fake_session_execute

        # Start a background command
        result = await sandbox.execute_bash_command(
            "sleep 999",
            background=True,
        )

        assert result["success"] is True
        assert "bg-cmd-001" in result["stdout"]

        # Verify the session was tracked
        cmd_id = "bg-cmd-001"
        assert cmd_id in sandbox._bg_sessions

        # Stop the background command
        stopped = await sandbox.stop_background_command(cmd_id)

        assert stopped is True
        assert cmd_id not in sandbox._bg_sessions
        # The session should have been deleted
        assert any("bg-" in s for s in deleted_sessions)

    async def test_stop_unknown_command_returns_false(self, sandbox):
        """Stopping a non-existent command ID returns False."""
        result = await sandbox.stop_background_command("nonexistent-cmd")
        assert result is False


class TestGetBackgroundCommandStatus:
    """PTCSandbox.get_background_command_status with mocked sessions."""

    async def test_status_of_running_command(self, sandbox):
        """Check status of a running background command."""
        async def fake_create_session(session_id):
            pass

        async def fake_session_execute(session_id, command, *, run_async=False, timeout=None):
            return SessionCommandResult(
                cmd_id="bg-cmd-001", exit_code=None, stdout="", stderr="",
            )

        async def fake_session_command_logs(session_id, cmd_id):
            return SessionCommandResult(
                cmd_id=cmd_id,
                exit_code=None,  # still running
                stdout="working...\n",
                stderr="",
            )

        sandbox.runtime.create_session = fake_create_session
        sandbox.runtime.session_execute = fake_session_execute
        sandbox.runtime.session_command_logs = fake_session_command_logs
        sandbox.runtime.delete_session = AsyncMock()

        await sandbox.execute_bash_command("sleep 999", background=True)
        cmd_id = "bg-cmd-001"

        status = await sandbox.get_background_command_status(cmd_id)

        assert status["is_running"] is True
        assert status["cmd_id"] == cmd_id
        assert "working" in status["stdout"]
        # Session should NOT be cleaned up yet
        assert cmd_id in sandbox._bg_sessions

    async def test_status_of_completed_command_auto_cleans(self, sandbox):
        """Completed command status auto-cleans the session."""
        deleted_sessions = []

        async def fake_create_session(session_id):
            pass

        async def fake_delete_session(session_id):
            deleted_sessions.append(session_id)

        async def fake_session_execute(session_id, command, *, run_async=False, timeout=None):
            return SessionCommandResult(
                cmd_id="bg-cmd-001", exit_code=None, stdout="", stderr="",
            )

        async def fake_session_command_logs(session_id, cmd_id):
            return SessionCommandResult(
                cmd_id=cmd_id,
                exit_code=0,  # completed
                stdout="done\n",
                stderr="",
            )

        sandbox.runtime.create_session = fake_create_session
        sandbox.runtime.delete_session = fake_delete_session
        sandbox.runtime.session_execute = fake_session_execute
        sandbox.runtime.session_command_logs = fake_session_command_logs

        await sandbox.execute_bash_command("echo hello", background=True)
        cmd_id = "bg-cmd-001"

        status = await sandbox.get_background_command_status(cmd_id)

        assert status["is_running"] is False
        assert status["success"] is True
        assert status["exit_code"] == 0
        # Session should be auto-cleaned
        assert cmd_id not in sandbox._bg_sessions
        assert len(deleted_sessions) > 0

    async def test_status_of_unknown_command(self, sandbox):
        """Querying status for an unknown command returns a failure dict."""
        status = await sandbox.get_background_command_status("nonexistent")

        assert status["success"] is False
        assert status["is_running"] is False
        assert "No background session" in status["stderr"]
