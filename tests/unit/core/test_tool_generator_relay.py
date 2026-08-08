"""Tests for relay-bound (OAuth) MCP server codegen in tool_generator.

Pins the egress-relay client contract: OAuth servers dial the relay with the
sandbox's relay JWT (never a vendor URL or token), and source='user' servers
get the untrusted vault-only treatment. The client itself is a static runtime
module; the security contract lives in the generated CONFIG epilogue.
"""

import ast
import json
import re

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
    def test_version_shape(self):
        # Warm sandboxes cache generated wrappers by this version. The hash
        # suffix tracks the static runtime file automatically; the major is a
        # deliberate wrapper/composition-logic bump ("5" = runtime extraction).
        assert re.fullmatch(r"5\.[0-9a-f]{12}", MCP_CLIENT_CODEGEN_VERSION)

    def test_version_tracks_runtime_content(self):
        # The suffix is a content hash of mcp_client_runtime.py — editing the
        # runtime reaches warm sandboxes with no manual bump.
        import hashlib

        from ptc_agent.core.tool_generator import client_runtime_source

        expected = hashlib.sha256(
            client_runtime_source().encode("utf-8")
        ).hexdigest()[:12]
        assert MCP_CLIENT_CODEGEN_VERSION.endswith(expected)


class TestRelayBoundEmission:
    def test_oauth_server_carries_no_vendor_url(self):
        gen = ToolFunctionGenerator()
        config = gen.generate_client_config([_oauth_server()], working_dir="/work")
        entry = config["servers"]["rh_srv"]
        # The vendor destination lives host-side only.
        assert entry == {
            "transport": "http",
            "source": "user",
            "relay_bound": True,
            "relay_grant_id": "grant-abc",
        }
        code = gen.generate_mcp_client_code([_oauth_server()], working_dir="/work")
        assert "vendor.example.com" not in code

    def test_relay_helpers_present(self):
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

    def test_builtin_only_config_has_no_relay_binding(self):
        # The runtime module always ships the relay/vault helpers (static
        # file); what a builtin-only workspace must never get is a relay-bound
        # or untrusted CONFIG entry.
        gen = ToolFunctionGenerator()
        builtin = MCPServerConfig(
            name="data_srv", transport="stdio", command="node", args=["srv.js"]
        )
        config = gen.generate_client_config([builtin])
        entry = config["servers"]["data_srv"]
        assert "relay_bound" not in entry
        assert "source" not in entry
        assert "env" not in entry


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
        config = gen.generate_client_config([srv], working_dir=str(tmp_path))
        assert config["servers"]["plain_user_srv"]["source"] == "user"
        ns = _exec_client(
            gen.generate_mcp_client_code([srv], working_dir=str(tmp_path))
        )
        url, headers = ns["_resolve_sse"](
            ns["_SERVER_CONFIGS"]["plain_user_srv"], "plain_user_srv"
        )
        assert url == "https://api.example.test/mcp"
        assert headers["Authorization"] == "Bearer sekret"
