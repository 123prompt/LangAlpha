"""Configured connector headers must not override protocol-owned MCP headers.

The config model's key regex allows hyphens, so nothing upstream stops a
workspace server entry from carrying ``MCP-Protocol-Version`` or a forged
``Mcp-Session-Id`` — and a plain-dict merge would even emit both casings of
the same name on the wire. The strip in ``_mcp_headers`` is the one guard.
"""

from ptc_agent.core.sandbox import mcp_client_runtime as m


class TestReservedHeaderStrip:
    def test_modern_protocol_headers_win_over_configured_ones(self):
        proto = {"mode": "modern", "version": "2026-07-28", "session_id": None}

        headers = m._mcp_headers(
            "tools/call",
            "get_quote",
            proto,
            {
                "MCP-Protocol-Version": "1999-01-01",
                "mcp-method": "tools/other",
                "Mcp-Name": "spoofed",
                "Mcp-Session-Id": "forged",
                "X-Api-Key": "k-123",
            },
        )

        assert headers["MCP-Protocol-Version"] == "2026-07-28"
        assert headers["Mcp-Method"] == "tools/call"
        assert headers["Mcp-Name"] == "get_quote"
        # No alternate-casing duplicate survives to be sent alongside.
        assert "mcp-method" not in headers
        # Modern mode has no session; a configured one must not invent it.
        assert not any(k.lower() == "mcp-session-id" for k in headers)
        assert headers["X-Api-Key"] == "k-123"

    def test_legacy_session_id_cannot_be_forged(self):
        proto = {"mode": "legacy", "version": "2025-11-25", "session_id": "s-live"}

        headers = m._mcp_headers(
            "tools/call", "", proto, {"mcp-session-id": "forged"}
        )

        assert headers["Mcp-Session-Id"] == "s-live"
        assert "mcp-session-id" not in headers

    def test_non_reserved_headers_still_apply_last(self):
        # The override lane stays open for everything the protocol does not
        # own — Accept included, for servers with quirky content negotiation.
        proto = {"mode": "modern", "version": "2026-07-28", "session_id": None}

        headers = m._mcp_headers(
            "tools/list", "", proto, {"Accept": "application/json"}
        )

        assert headers["Accept"] == "application/json"
