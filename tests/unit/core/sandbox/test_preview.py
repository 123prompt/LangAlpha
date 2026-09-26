"""
Tests for PTCSandbox preview methods and workspace_sandbox preview redirect endpoint.

Part 1: PTCSandbox unit tests covering background session management,
         preview server lifecycle, reachability checks, and leak fixes.
Part 2: preview URL resolution, and the legacy preview redirect to the
         app's ``/a/`` link.
"""

import asyncio
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from ptc_agent.config.core import (
    CoreConfig,
    DaytonaConfig,
    FilesystemConfig,
    LoggingConfig,
    MCPConfig,
    SandboxConfig,
    SecurityConfig,
)
from ptc_agent.core.sandbox.runtime import (
    PreviewInfo,
    SessionCommandResult,
    SandboxProvider,
    SandboxRuntime,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> CoreConfig:
    defaults = dict(
        sandbox=SandboxConfig(daytona=DaytonaConfig(api_key="test-key")),
        security=SecurityConfig(),
        mcp=MCPConfig(),
        logging=LoggingConfig(),
        filesystem=FilesystemConfig(),
    )
    defaults.update(overrides)
    return CoreConfig(**defaults)


def _make_sandbox(mock_provider, mock_runtime):
    """Create a ready PTCSandbox wired to mocks."""
    from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

    with patch(
        "ptc_agent.core.sandbox.ptc_sandbox.create_provider",
        return_value=mock_provider,
    ):
        sandbox = PTCSandbox(config=_make_config())
    sandbox.runtime = mock_runtime
    sandbox._ready_event = asyncio.Event()
    sandbox._ready_event.set()
    return sandbox


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_runtime():
    runtime = AsyncMock(spec=SandboxRuntime)
    runtime.id = "test-sandbox"
    runtime.working_dir = "/workspace"
    runtime.create_session = AsyncMock()
    runtime.delete_session = AsyncMock()
    runtime.session_execute = AsyncMock(
        return_value=SessionCommandResult(
            cmd_id="cmd-001", exit_code=None, stdout="", stderr=""
        )
    )
    runtime.session_command_logs = AsyncMock(
        return_value=SessionCommandResult(
            cmd_id="cmd-001", exit_code=None, stdout="some output", stderr=""
        )
    )
    runtime.exec = AsyncMock()
    runtime.upload_file = AsyncMock()
    runtime.get_preview_url = AsyncMock(
        return_value=PreviewInfo(url="https://preview.example.com/signed", token="tok")
    )
    runtime.get_preview_link = AsyncMock(
        return_value=PreviewInfo(
            url="https://preview.example.com/link",
            token="tok",
            auth_headers={"Authorization": "Bearer tok"},
        )
    )
    return runtime


@pytest.fixture
def mock_provider():
    provider = AsyncMock(spec=SandboxProvider)
    provider.is_transient_error = MagicMock(return_value=False)
    provider.close = AsyncMock()
    return provider


@pytest.fixture
def sandbox(mock_provider, mock_runtime):
    return _make_sandbox(mock_provider, mock_runtime)


# ===================================================================
# Part 1: PTCSandbox unit tests
# ===================================================================


class TestCreateBgSession:
    """Tests for PTCSandbox._create_bg_session."""

    @pytest.mark.asyncio
    async def test_happy_path_creates_session(self, sandbox, mock_runtime):
        session_id = await sandbox._create_bg_session("task-1")
        assert session_id == "bg-task-1"
        mock_runtime.create_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_already_exists_triggers_delete_and_recreate(
        self, sandbox, mock_runtime
    ):
        mock_runtime.create_session.side_effect = [
            Exception("session already exists"),
            None,  # recreate succeeds
        ]
        mock_runtime.delete_session.return_value = None

        session_id = await sandbox._create_bg_session("task-2")

        assert session_id == "bg-task-2"
        assert mock_runtime.delete_session.call_count == 1
        assert mock_runtime.create_session.call_count == 2

    @pytest.mark.asyncio
    async def test_delete_recreate_failure_reuses_stale(
        self, sandbox, mock_runtime
    ):
        mock_runtime.create_session.side_effect = [
            Exception("session already exists"),
            Exception("still broken"),
        ]
        mock_runtime.delete_session.side_effect = Exception("delete also failed")

        # Should not raise — falls back to reusing stale session
        session_id = await sandbox._create_bg_session("task-3")
        assert session_id == "bg-task-3"

    @pytest.mark.asyncio
    async def test_non_already_exists_error_raises(self, sandbox, mock_runtime):
        mock_runtime.create_session.side_effect = Exception("permission denied")

        with pytest.raises(Exception, match="permission denied"):
            await sandbox._create_bg_session("task-4")


class TestStopBackgroundCommand:
    """Tests for PTCSandbox.stop_background_command."""

    @pytest.mark.asyncio
    async def test_found_and_deleted(self, sandbox, mock_runtime):
        sandbox._bg_sessions["cmd-abc"] = "bg-abc"
        result = await sandbox.stop_background_command("cmd-abc")
        assert result is True
        mock_runtime.delete_session.assert_called_once()
        assert "cmd-abc" not in sandbox._bg_sessions

    @pytest.mark.asyncio
    async def test_no_session_returns_false(self, sandbox):
        result = await sandbox.stop_background_command("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_fails_returns_false(self, sandbox, mock_runtime):
        sandbox._bg_sessions["cmd-fail"] = "bg-fail"
        mock_runtime.delete_session.side_effect = Exception("network error")

        result = await sandbox.stop_background_command("cmd-fail")
        assert result is False
        # Session should be cleaned up from _bg_sessions even on failure
        assert "cmd-fail" not in sandbox._bg_sessions


class TestStartPreviewServer:
    """Tests for PTCSandbox.start_preview_server."""

    @pytest.mark.asyncio
    async def test_happy_path_creates_per_port_session(self, sandbox, mock_runtime):
        mock_runtime.session_execute.return_value = SessionCommandResult(
            cmd_id="preview-cmd-1", exit_code=None, stdout="", stderr=""
        )

        cmd_id = await sandbox.start_preview_server("python -m http.server 8080", 8080)

        assert cmd_id == "preview-cmd-1"
        mock_runtime.create_session.assert_called_once()
        # Verify the session is stored correctly
        assert 8080 in sandbox._preview_sessions
        session_id, stored_cmd_id = sandbox._preview_sessions[8080]
        assert session_id.startswith("preview-8080-")
        assert stored_cmd_id == "preview-cmd-1"

    @pytest.mark.asyncio
    async def test_stale_session_teardown(self, sandbox, mock_runtime):
        # Pre-populate a stale session for port 8080
        sandbox._preview_sessions[8080] = ("preview-8080", "old-cmd")

        mock_runtime.session_execute.return_value = SessionCommandResult(
            cmd_id="new-cmd", exit_code=None, stdout="", stderr=""
        )

        cmd_id = await sandbox.start_preview_server("python app.py", 8080)

        assert cmd_id == "new-cmd"
        # delete_session should have been called for the stale session
        assert mock_runtime.delete_session.call_count >= 1
        # Verify updated preview sessions
        assert sandbox._preview_sessions[8080][0].startswith("preview-8080-")
        assert sandbox._preview_sessions[8080][1] == "new-cmd"

    @pytest.mark.asyncio
    async def test_stale_session_cleanup_failure_continues(
        self, sandbox, mock_runtime
    ):
        sandbox._preview_sessions[8080] = ("preview-8080", "old-cmd")
        # delete_session fails but we should continue
        mock_runtime.delete_session.side_effect = Exception("cleanup failed")

        mock_runtime.session_execute.return_value = SessionCommandResult(
            cmd_id="new-cmd", exit_code=None, stdout="", stderr=""
        )

        cmd_id = await sandbox.start_preview_server("python app.py", 8080)
        assert cmd_id == "new-cmd"

    @pytest.mark.asyncio
    async def test_already_exists_on_create_continues(self, sandbox, mock_runtime):
        mock_runtime.create_session.side_effect = Exception(
            "Session already exists for this sandbox"
        )
        mock_runtime.session_execute.return_value = SessionCommandResult(
            cmd_id="reused-cmd", exit_code=None, stdout="", stderr=""
        )

        cmd_id = await sandbox.start_preview_server("node server.js", 3000)
        assert cmd_id == "reused-cmd"


class TestIsPreviewReachable:
    """Tests for PTCSandbox._is_preview_reachable."""

    @pytest.mark.asyncio
    async def test_returns_true_for_200(self, sandbox, mock_runtime):
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.head = AsyncMock(return_value=mock_response)
            MockClient.return_value.__aenter__ = AsyncMock(
                return_value=client_instance
            )
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await sandbox._is_preview_reachable(8080)
            assert result is True

    @pytest.mark.asyncio
    async def test_returns_true_for_404(self, sandbox, mock_runtime):
        """404 means server IS running but path not found."""
        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch("httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.head = AsyncMock(return_value=mock_response)
            MockClient.return_value.__aenter__ = AsyncMock(
                return_value=client_instance
            )
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await sandbox._is_preview_reachable(8080)
            assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_for_502(self, sandbox, mock_runtime):
        """502 means proxy can't reach the backend."""
        mock_response = MagicMock()
        mock_response.status_code = 502

        with patch("httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.head = AsyncMock(return_value=mock_response)
            MockClient.return_value.__aenter__ = AsyncMock(
                return_value=client_instance
            )
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await sandbox._is_preview_reachable(8080)
            assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_for_503(self, sandbox, mock_runtime):
        mock_response = MagicMock()
        mock_response.status_code = 503

        with patch("httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.head = AsyncMock(return_value=mock_response)
            MockClient.return_value.__aenter__ = AsyncMock(
                return_value=client_instance
            )
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await sandbox._is_preview_reachable(8080)
            assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self, sandbox, mock_runtime):
        mock_runtime.get_preview_link.side_effect = Exception("connection refused")

        result = await sandbox._is_preview_reachable(8080)
        assert result is False


class TestStopPreviewServer:
    """Tests for PTCSandbox.stop_preview_server."""

    @pytest.mark.asyncio
    async def test_found_and_deleted(self, sandbox, mock_runtime):
        sandbox._preview_sessions[8080] = ("preview-8080", "cmd-1")
        result = await sandbox.stop_preview_server(8080)
        assert result is True
        mock_runtime.delete_session.assert_called_once()
        assert 8080 not in sandbox._preview_sessions

    @pytest.mark.asyncio
    async def test_not_found_returns_false(self, sandbox):
        result = await sandbox.stop_preview_server(9999)
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_fails_still_cleans_up(self, sandbox, mock_runtime):
        sandbox._preview_sessions[8080] = ("preview-8080", "cmd-1")
        mock_runtime.delete_session.side_effect = Exception("network error")

        result = await sandbox.stop_preview_server(8080)
        # Returns True even if delete fails (session entry is still cleaned up)
        assert result is True
        assert 8080 not in sandbox._preview_sessions


class TestGetPreviewServerLogs:
    """Tests for PTCSandbox.get_preview_server_logs."""

    @pytest.mark.asyncio
    async def test_entry_exists_returns_logs(self, sandbox, mock_runtime):
        sandbox._preview_sessions[8080] = ("preview-8080", "cmd-1")
        mock_runtime.session_command_logs.return_value = SessionCommandResult(
            cmd_id="cmd-1", exit_code=None, stdout="server started", stderr=""
        )

        result = await sandbox.get_preview_server_logs(8080)
        assert result["success"] is True
        assert result["is_running"] is True
        assert result["stdout"] == "server started"
        assert result["port"] == 8080

    @pytest.mark.asyncio
    async def test_no_entry_returns_error_dict(self, sandbox):
        result = await sandbox.get_preview_server_logs(9999)
        assert result["success"] is False
        assert result["is_running"] is False
        assert "No preview session" in result["stderr"]
        assert result["port"] == 9999

    @pytest.mark.asyncio
    async def test_logs_api_failure_returns_error_dict(self, sandbox, mock_runtime):
        sandbox._preview_sessions[8080] = ("preview-8080", "cmd-1")
        mock_runtime.session_command_logs.side_effect = Exception("API error")

        result = await sandbox.get_preview_server_logs(8080)
        assert result["success"] is False
        assert "Failed to get logs" in result["stderr"]


class TestExecuteBashBackgroundSessionLeakFix:
    """Test that session_execute failure triggers session cleanup.

    Note: execute_bash_command has an outer try/except that catches all
    exceptions and returns an error dict, so the exception does not propagate.
    We verify the cleanup occurred by checking delete_session was called and
    the returned dict indicates failure.
    """

    @pytest.mark.asyncio
    async def test_session_execute_failure_cleans_up_session(
        self, sandbox, mock_runtime
    ):
        # Make create_session succeed but session_execute fail
        mock_runtime.create_session.return_value = None
        mock_runtime.session_execute.side_effect = Exception("execute failed")
        mock_runtime.delete_session.return_value = None

        result = await sandbox.execute_bash_command(
            "sleep 100", background=True
        )

        # The outer except catches and returns an error dict
        assert result["success"] is False
        assert result["exit_code"] == -1
        # Verify the session was cleaned up after execute failure
        mock_runtime.delete_session.assert_called()

    @pytest.mark.asyncio
    async def test_session_cleanup_failure_does_not_mask_original_error(
        self, sandbox, mock_runtime
    ):
        mock_runtime.create_session.return_value = None
        mock_runtime.session_execute.side_effect = Exception("execute failed")
        mock_runtime.delete_session.side_effect = Exception("cleanup also failed")

        result = await sandbox.execute_bash_command(
            "sleep 100", background=True
        )

        # Should still return error dict even when cleanup also fails
        assert result["success"] is False
        assert "execute failed" in result["stderr"] or result["exit_code"] == -1


# ===================================================================
# Part 2: preview resolution and the legacy redirect
# ===================================================================


def _make_workspace(status="running", **overrides):
    ws = {
        "id": "ws-test-001",
        "user_id": "test-user-123",
        "workspace_id": "ws-test-001",
        "status": status,
        "sandbox_id": "sb-123",
        "computer_root_dir": "/home/workspace",
        "dir_name": "project-a",
        "created_at": "2026-01-01T00:00:00Z",
    }
    ws.update(overrides)
    return ws


@pytest.fixture
def mock_sandbox_for_endpoint():
    sandbox = AsyncMock()
    sandbox.sandbox_id = "sb-123"
    sandbox.start_and_get_preview_url = AsyncMock(
        return_value=PreviewInfo(
            url="https://preview.example.com/signed?token=abc", token="abc"
        )
    )
    sandbox.get_preview_url = AsyncMock(
        return_value=PreviewInfo(
            url="https://preview.example.com/signed?token=abc", token="abc"
        )
    )
    return sandbox


@pytest.fixture
def mock_session_for_endpoint(mock_sandbox_for_endpoint):
    session = MagicMock()
    session.sandbox = mock_sandbox_for_endpoint
    return session


class TestWorkspaceScopedPreviewCommands:
    @pytest.fixture(autouse=True)
    def _available_preview_coordination(self):
        cache = MagicMock()
        cache.acquire_lock = AsyncMock(return_value=True)
        cache.release_lock = AsyncMock()
        cache.get = AsyncMock(return_value=None)
        cache.set = AsyncMock(return_value=True)
        cache.delete = AsyncMock()
        with patch(
            "src.server.app.workspace_sandbox.get_cache_client",
            return_value=cache,
        ):
            yield

    @pytest.mark.asyncio
    async def test_stored_command_restarts_inside_the_workspace_folder(
        self, mock_sandbox_for_endpoint
    ):
        from src.server.app.workspace_sandbox import _resolve_preview

        with patch(
            "src.server.app.workspace_sandbox._set_cached_signed_url",
            AsyncMock(),
        ):
            await _resolve_preview(
                mock_sandbox_for_endpoint,
                "ws-test-001",
                8080,
                command="python -m http.server 8080",
                force=True,
                work_dir="/home/workspace/project-a",
            )

        mock_sandbox_for_endpoint.start_and_get_preview_url.assert_awaited_once_with(
            "cd /home/workspace/project-a && python -m http.server 8080",
            8080,
            expires_in=3600,
            owner="ws-test-001",
        )

    @pytest.mark.asyncio
    async def test_explicit_restart_runs_inside_the_workspace_folder(
        self, mock_sandbox_for_endpoint
    ):
        from src.server.app.workspace_sandbox import (
            PreviewRestartRequest,
            restart_preview_server,
        )

        with (
            patch(
                "src.server.app.workspace_sandbox._get_sandbox",
                AsyncMock(return_value=(MagicMock(), mock_sandbox_for_endpoint)),
            ),
            patch(
                "src.server.app.workspace_sandbox.db_get_workspace",
                AsyncMock(return_value=_make_workspace()),
            ),
        ):
            response = await restart_preview_server(
                "ws-test-001",
                "test-user-123",
                PreviewRestartRequest(
                    port=8080, command="python -m http.server 8080"
                ),
            )

        assert response.success is True
        mock_sandbox_for_endpoint.start_preview_server.assert_awaited_once_with(
            "cd /home/workspace/project-a && python -m http.server 8080",
            8080,
            owner="ws-test-001",
        )

    @pytest.mark.asyncio
    async def test_cache_miss_reuses_owned_preview_from_another_worker(
        self, mock_sandbox_for_endpoint
    ):
        from src.server.app.workspace_sandbox import _resolve_preview

        mock_sandbox_for_endpoint._is_preview_reachable = AsyncMock(return_value=True)
        with (
            patch(
                "src.server.app.workspace_sandbox._get_preview_owner",
                AsyncMock(return_value="ws-test-001"),
            ),
            patch(
                "src.server.app.workspace_sandbox._get_cached_signed_url",
                AsyncMock(return_value=None),
            ),
            patch(
                "src.server.app.workspace_sandbox._set_cached_signed_url",
                AsyncMock(),
            ) as cache_url,
        ):
            url = await _resolve_preview(
                mock_sandbox_for_endpoint,
                "ws-test-001",
                8080,
                command="python -m http.server 8080",
                work_dir="/home/workspace/project-a",
            )

        assert url == "https://preview.example.com/signed?token=abc"
        mock_sandbox_for_endpoint.start_and_get_preview_url.assert_not_awaited()
        cache_url.assert_awaited_once_with(
            "sb-123",
            8080,
            "https://preview.example.com/signed?token=abc",
            expires_in=60,
            owner_workspace_id="ws-test-001",
            url_expires_at=ANY,
        )

    @pytest.mark.asyncio
    async def test_cross_worker_launch_reserves_owner_before_provider_work(self):
        from src.server.app.workspace_sandbox import _resolve_preview

        class Cache:
            def __init__(self):
                self.values = {}
                self.locks = {}

            async def acquire_lock(self, key, token, _ttl_ms):
                if key in self.locks:
                    return False
                self.locks[key] = token
                return True

            async def release_lock(self, key, token):
                if self.locks.get(key) == token:
                    self.locks.pop(key)

            async def get(self, key):
                return self.values.get(key)

            async def set(self, key, value, ttl=None):
                self.values[key] = value
                return True

            async def delete(self, key):
                self.values.pop(key, None)

        cache = Cache()
        launch_entered = asyncio.Event()
        finish_launch = asyncio.Event()
        first = AsyncMock()
        first.sandbox_id = "shared-sandbox"

        async def launch(*_args, **_kwargs):
            assert cache.values["preview:owner:shared-sandbox:8080"] == "workspace-a"
            launch_entered.set()
            await finish_launch.wait()
            return PreviewInfo(url="https://preview.example.com/a", token="a")

        first.start_and_get_preview_url = AsyncMock(side_effect=launch)
        second = AsyncMock()
        second.sandbox_id = "shared-sandbox"
        second.start_and_get_preview_url = AsyncMock()

        with (
            patch(
                "src.server.app.workspace_sandbox.get_cache_client",
                return_value=cache,
            ),
            patch(
                "src.server.app.workspace_sandbox._check_signed_url_healthy",
                AsyncMock(return_value=True),
            ),
        ):
            first_task = asyncio.create_task(_resolve_preview(
                first,
                "workspace-a",
                8080,
                command="python -m http.server 8080",
                work_dir="/home/workspace/a",
            ))
            await launch_entered.wait()
            second_task = asyncio.create_task(_resolve_preview(
                second,
                "workspace-b",
                8080,
                command="python -m http.server 8080",
                work_dir="/home/workspace/b",
            ))
            await asyncio.sleep(0.1)
            second.start_and_get_preview_url.assert_not_awaited()
            finish_launch.set()
            assert await first_task == "https://preview.example.com/a"
            with pytest.raises(RuntimeError, match="already in use"):
                await second_task

    @pytest.mark.asyncio
    async def test_failed_launch_releases_owner_reservation(
        self, mock_sandbox_for_endpoint
    ):
        from src.server.app.workspace_sandbox import _resolve_preview

        cache = MagicMock()
        cache.acquire_lock = AsyncMock(return_value=True)
        cache.release_lock = AsyncMock()
        values = {}
        cache.get = AsyncMock(side_effect=lambda key: values.get(key))
        cache.set = AsyncMock(side_effect=lambda key, value, ttl=None: values.__setitem__(key, value) or True)
        cache.delete = AsyncMock(side_effect=lambda key: values.pop(key, None))
        mock_sandbox_for_endpoint.start_and_get_preview_url.side_effect = RuntimeError("boom")

        with patch(
            "src.server.app.workspace_sandbox.get_cache_client",
            return_value=cache,
        ):
            with pytest.raises(RuntimeError, match="boom"):
                await _resolve_preview(
                    mock_sandbox_for_endpoint,
                    "workspace-a",
                    8080,
                    command="python -m http.server 8080",
                    work_dir="/home/workspace/a",
                )

        assert "preview:owner:sb-123:8080" not in values
        cache.release_lock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_commandless_redirect_rejects_a_sibling_preview(
        self, mock_sandbox_for_endpoint
    ):
        from src.server.app.workspace_sandbox import _resolve_preview

        mock_sandbox_for_endpoint._is_preview_reachable = AsyncMock(return_value=True)
        with (
            patch(
                "src.server.app.workspace_sandbox._get_preview_owner",
                AsyncMock(return_value="workspace-b"),
            ),
            patch(
                "src.server.app.workspace_sandbox._get_cached_signed_url",
                AsyncMock(return_value="https://preview.example.com/b"),
            ),
        ):
            with pytest.raises(RuntimeError, match="already in use"):
                await _resolve_preview(
                    mock_sandbox_for_endpoint,
                    "workspace-a",
                    8080,
                    command=None,
                    work_dir="/home/workspace/a",
                )

        mock_sandbox_for_endpoint.get_preview_url.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_launch_fails_closed_without_preview_coordination(
        self, mock_sandbox_for_endpoint
    ):
        from src.server.app.workspace_sandbox import _resolve_preview

        cache = MagicMock()
        cache.acquire_lock = AsyncMock(return_value=None)
        with patch(
            "src.server.app.workspace_sandbox.get_cache_client",
            return_value=cache,
        ):
            with pytest.raises(RuntimeError, match="coordination is unavailable"):
                await _resolve_preview(
                    mock_sandbox_for_endpoint,
                    "workspace-a",
                    8080,
                    command="python -m http.server 8080",
                    work_dir="/home/workspace/a",
                )

        mock_sandbox_for_endpoint.start_and_get_preview_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_signed_urls_expiry_is_counted_from_its_mint_not_from_the_read():
    """The ``/a/`` page renews an app's URL off this, and a cached URL is older
    than the request that gets it back."""
    from src.server.app import workspace_sandbox as ws

    store: dict = {}
    cache = MagicMock()
    cache.set = AsyncMock(side_effect=lambda key, value, ttl=None: store.__setitem__(key, value))
    cache.get = AsyncMock(side_effect=lambda key: store.get(key))
    with (
        patch.object(ws, "get_cache_client", return_value=cache),
        patch.object(ws.time, "time", return_value=1_000_000),
    ):
        await ws._set_cached_signed_url("sb-1", 8080, "https://a", url_expires_at=1_003_600)
        await ws._set_cached_signed_url("sb-1", 8081, "https://b")
        assert await ws.signed_url_expires_at("https://a") == 1_003_600
        assert await ws.signed_url_expires_at("https://b") == 1_000_000 + 3600
        # Cached by a worker that kept no record: the cache's own margin.
        assert await ws.signed_url_expires_at("https://c") == 1_000_000 + 600


class TestPreviewRedirectEndpoint:
    """The legacy ``/api/v1/preview/{ws}/{port}`` route: a redirect to the
    app's ``/a/`` link that never touches a session or the sandbox."""

    _COMMAND = "src.server.app.workspace_sandbox.get_preview_command"
    _LINK = "src.server.app.workspace_sandbox.ensure_app_link"
    _WSMGR = "src.server.app.workspace_sandbox.WorkspaceManager"

    @staticmethod
    def _client():
        from httpx import ASGITransport, AsyncClient

        from src.server.app.workspace_sandbox import preview_redirect_router
        from tests.conftest import create_test_app

        app = create_test_app(preview_redirect_router)
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    @pytest.mark.asyncio
    async def test_no_server_on_the_port_returns_404(self):
        """An unknown workspace and an unregistered port read the same."""
        with (
            patch(self._COMMAND, AsyncMock(return_value=None)),
            patch(self._LINK, AsyncMock()) as link,
        ):
            async with self._client() as client:
                resp = await client.get(
                    "/api/v1/preview/ws-test-001/8080", follow_redirects=False
                )
        assert resp.status_code == 404
        link.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("suffix", "query"),
        [
            ("", "?path=/"),
            ("/timeline.html", "?path=timeline.html"),
            ("/reports/q3.html", "?path=reports/q3.html"),
        ],
    )
    async def test_registered_port_redirects_to_the_link_without_a_session(self, suffix, query):
        """#378: the redirect never acquires a session, so a manager and a
        sandbox lookup that both blow up leave the answer untouched."""
        from src.config.env import PUBLIC_APP_URL
        from src.server.database.share_links import ShareLink

        link = MagicMock(spec=ShareLink, code="k3Vq9ZtR2mXa")
        with (
            patch(self._COMMAND, AsyncMock(return_value="python -m http.server 8080")),
            patch(self._LINK, AsyncMock(return_value=link)) as ensure,
            patch(self._WSMGR) as manager,
            patch(
                "src.server.app.workspace_sandbox._get_sandbox",
                AsyncMock(side_effect=RuntimeError("no session for you")),
            ),
        ):
            manager.get_instance.side_effect = RuntimeError("manager is down")
            async with self._client() as client:
                resp = await client.get(
                    f"/api/v1/preview/ws-test-001/8080{suffix}", follow_redirects=False
                )
        assert resp.status_code == 302
        # The old URL's suffix rides the redirect; it is never stored on the
        # link, which anyone holding the UUID could otherwise choose.
        assert resp.headers["location"] == f"{PUBLIC_APP_URL}/a/k3Vq9ZtR2mXa{query}"
        assert resp.headers["cache-control"] == "no-store"
        ensure.assert_awaited_once_with("ws-test-001", 8080)
        manager.get_instance.assert_not_called()


_SIGNED = "https://8050-sbx.proxy.test/?token=abc"


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        (None, _SIGNED),
        ("/", _SIGNED),
        ("reports/q3.html", "https://8050-sbx.proxy.test/reports/q3.html?token=abc"),
        # The page's query follows the signed one, which is what admits the request.
        ("dashboard?tab=positions", "https://8050-sbx.proxy.test/dashboard?token=abc&tab=positions"),
        ("dashboard#top", "https://8050-sbx.proxy.test/dashboard?token=abc#top"),
        ("/?tab=a b", "https://8050-sbx.proxy.test/?token=abc&tab=a+b"),
    ],
)
def test_a_page_opens_inside_the_signed_url_without_displacing_its_token(page, expected):
    from src.server.app.workspace_sandbox import with_preview_path

    assert with_preview_path(_SIGNED, page) == expected


@pytest.mark.asyncio
async def test_occupied_preview_port_never_returns_sibling_url(sandbox, mock_runtime):
    mock_runtime.exec.return_value = MagicMock(exit_code=0)
    with pytest.raises(RuntimeError, match="already in use"):
        await sandbox.start_and_get_preview_url("python -m http.server 8080", 8080)
    mock_runtime.session_execute.assert_not_awaited()
    mock_runtime.get_preview_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_occupied_preview_port_reuses_the_owning_workspace_server(
    sandbox, mock_runtime
):
    sandbox._preview_sessions[8080] = ("preview-8080", "cmd-1")
    sandbox._preview_owners[8080] = "ws-1"
    mock_runtime.exec.return_value = MagicMock(exit_code=0)
    with patch.object(sandbox, "_is_preview_reachable", AsyncMock(return_value=True)):
        result = await sandbox.start_and_get_preview_url(
            "python -m http.server 8080", 8080, owner="ws-1"
        )

    assert result.url == "https://preview.example.com/signed"
    mock_runtime.session_execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_occupied_preview_port_rejects_a_sibling_workspace(
    sandbox, mock_runtime
):
    sandbox._preview_sessions[8080] = ("preview-8080", "cmd-1")
    sandbox._preview_owners[8080] = "ws-1"
    mock_runtime.exec.return_value = MagicMock(exit_code=0)
    with pytest.raises(RuntimeError, match="already in use"):
        await sandbox.start_and_get_preview_url(
            "python -m http.server 8080", 8080, owner="ws-2"
        )
    mock_runtime.get_preview_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_preview_command_never_returns_an_existing_server(sandbox, mock_runtime):
    mock_runtime.exec.side_effect = [MagicMock(exit_code=1), MagicMock(stdout="READY")]
    mock_runtime.session_command_logs.return_value = SessionCommandResult(
        cmd_id="cmd-001", exit_code=1, stdout="", stderr="Address already in use"
    )
    with pytest.raises(RuntimeError, match="Address already in use"):
        await sandbox.start_and_get_preview_url("python -m http.server 8080", 8080)
    mock_runtime.get_preview_url.assert_not_awaited()
