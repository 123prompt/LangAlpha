"""Tests for relay-bound (OAuth) MCP server codegen in tool_generator.

Pins the egress-relay client contract: OAuth servers dial the relay with the
sandbox's relay JWT (never a vendor URL or token), source='user' servers get
the untrusted vault-only treatment, and the builtin-only client stays free of
relay/vault machinery.
"""

import ast
import json

from ptc_agent.config.core import MCPServerConfig
from ptc_agent.core.tool_generator import (
    MCP_CLIENT_CODEGEN_VERSION,
    ToolFunctionGenerator,
)


def _exec_client(code: str) -> dict:
    """Compile + exec generated client source, returning its namespace."""
    ast.parse(code)
    ns: dict = {}
    exec(compile(code, "gen_mcp_client", "exec"), ns)  # noqa: S102 - testing generated code
    return ns


def _oauth_server(name: str = "rh_srv") -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        transport="http",
        url="https://vendor.example.com/mcp",
        source="user",
        oauth_connection_id="conn-1",
        egress_grant_id="grant-abc",
    )


def _write_relay_creds(tmp_path, payload: dict) -> str:
    internal = tmp_path / "_internal"
    internal.mkdir(parents=True, exist_ok=True)
    (internal / ".egress_relay.json").write_text(json.dumps(payload))
    return str(tmp_path)


class TestCodegenVersion:
    def test_version_pinned(self):
        # Warm sandboxes cache generated wrappers by this version; the relay
        # client shipped under "4" — a change here must be deliberate.
        assert MCP_CLIENT_CODEGEN_VERSION == "4"


class TestRelayBoundEmission:
    def test_oauth_server_carries_no_vendor_url(self):
        gen = ToolFunctionGenerator()
        code = gen.generate_mcp_client_code([_oauth_server()], working_dir="/work")
        # The vendor destination lives host-side only.
        assert "vendor.example.com" not in code
        assert '"relay_bound": True' in code
        assert "\"relay_grant_id\": 'grant-abc'" in code

    def test_relay_helpers_emitted(self):
        gen = ToolFunctionGenerator()
        code = gen.generate_mcp_client_code([_oauth_server()], working_dir="/work")
        for symbol in (
            "_EGRESS_RELAY_FILE",
            "_load_relay_credentials",
            "_resolve_relay",
            "_relay_error",
            "_RELAY_ERROR_HINTS",
        ):
            assert symbol in code, symbol
        # 401 recovery + typed reconnect errors read the machine-readable header.
        assert "x-relay-error" in code
        # Outer timeout sits above the relay's 55s wall.
        assert "httpx.Client(timeout=65.0)" in code

    def test_builtin_only_client_free_of_relay_and_vault_machinery(self):
        gen = ToolFunctionGenerator()
        builtin = MCPServerConfig(
            name="data_srv", transport="stdio", command="node", args=["srv.js"]
        )
        code = gen.generate_mcp_client_code([builtin])
        for symbol in ("_resolve_relay", "_relay_error", "relay_bound", "_load_vault"):
            assert symbol not in code, symbol


class TestRelayResolution:
    def test_resolves_relay_url_and_bearer_from_credential_file(self, tmp_path):
        workdir = _write_relay_creds(
            tmp_path,
            {
                "relay_base_url": "https://app.example.test",
                "token": "relay-jwt-token",
                "grants": {"rh_srv": "grant-abc"},
            },
        )
        gen = ToolFunctionGenerator()
        ns = _exec_client(
            gen.generate_mcp_client_code([_oauth_server()], working_dir=workdir)
        )
        url, headers = ns["_resolve_relay"](
            ns["_SERVER_CONFIGS"]["rh_srv"], "rh_srv"
        )
        assert url == "https://app.example.test/v1/egress/grant-abc"
        assert headers["Authorization"] == "Bearer relay-jwt-token"

    def test_missing_credential_file_raises_actionable_error(self, tmp_path):
        # No .egress_relay.json written — binding must fail clearly with no
        # network attempt, not fall back to a vendor URL.
        workdir = str(tmp_path)
        gen = ToolFunctionGenerator()
        ns = _exec_client(
            gen.generate_mcp_client_code([_oauth_server()], working_dir=workdir)
        )
        try:
            ns["_resolve_relay"](ns["_SERVER_CONFIGS"]["rh_srv"], "rh_srv")
        except Exception as e:
            assert "rh_srv" in str(e)
        else:  # pragma: no cover
            raise AssertionError("expected a binding error")


class TestUserSourceTreatment:
    def test_user_source_non_oauth_server_gets_vault_treatment(self, tmp_path):
        # A plain (non-OAuth) inherited server resolves headers vault-only,
        # exactly like a workspace server.
        internal = tmp_path / "_internal"
        internal.mkdir(parents=True, exist_ok=True)
        (internal / ".vault_secrets.json").write_text(json.dumps({"K": "sekret"}))
        srv = MCPServerConfig(
            name="plain_user_srv",
            transport="http",
            url="https://api.example.test/mcp",
            headers={"Authorization": "Bearer ${vault:K}"},
            source="user",
        )
        gen = ToolFunctionGenerator()
        code = gen.generate_mcp_client_code([srv], working_dir=str(tmp_path))
        ns = _exec_client(code)
        assert "\"source\": 'user'" in code
        url, headers = ns["_resolve_sse"](ns["_SERVER_CONFIGS"]["plain_user_srv"], "plain_user_srv")
        assert url == "https://api.example.test/mcp"
        assert headers["Authorization"] == "Bearer sekret"
