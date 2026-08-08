"""The HTTP reply reader must bound an untrusted (non-relay) MCP server.

httpx's read timeout resets on every byte, so a server that floods or drips
would otherwise OOM or hang the sandbox interpreter. ``_parse_http_reply`` /
``_read_body_capped`` cap the accumulated bytes and enforce a total-exchange
deadline that the per-read timeout can't provide.
"""

import time

import pytest

from ptc_agent.core.sandbox import mcp_client_runtime as m


class _FakeResp:
    """The subset of a streamed httpx response the reader touches."""

    def __init__(self, *, ctype="application/json", lines=None, body=b"", status=200):
        self.headers = {"content-type": ctype}
        self.status_code = status
        self._lines = lines or []
        self._body = body

    def iter_lines(self):
        yield from self._lines

    def iter_bytes(self):
        yield self._body

    def raise_for_status(self):
        pass


def _match_id() -> int:
    return 1


class TestByteCap:
    def test_an_oversized_sse_stream_without_a_reply_is_refused(self, monkeypatch):
        monkeypatch.setattr(m, "_HTTP_REPLY_MAX_BYTES", 64)
        resp = _FakeResp(
            ctype="text/event-stream",
            lines=[f"data: {i}" for i in range(200)],  # no blank line, no id match
        )
        with pytest.raises(RuntimeError, match="reply_too_large"):
            m._parse_http_reply(resp, _match_id(), "srv")

    def test_an_oversized_json_body_is_refused(self, monkeypatch):
        monkeypatch.setattr(m, "_HTTP_REPLY_MAX_BYTES", 64)
        resp = _FakeResp(ctype="application/json", body=b"x" * 4096)
        with pytest.raises(RuntimeError, match="reply_too_large"):
            m._parse_http_reply(resp, _match_id(), "srv")


class TestDeadline:
    def test_a_past_deadline_cuts_the_sse_read(self):
        resp = _FakeResp(
            ctype="text/event-stream",
            lines=['data: {"jsonrpc":"2.0","id":1,"result":{}}', ""],
        )
        with pytest.raises(RuntimeError, match="stream_deadline"):
            m._parse_http_reply(resp, 1, "srv", deadline=time.monotonic() - 1)

    def test_a_past_deadline_cuts_the_json_read(self):
        resp = _FakeResp(ctype="application/json", body=b"{}")
        with pytest.raises(RuntimeError, match="stream_deadline"):
            m._parse_http_reply(resp, 1, "srv", deadline=time.monotonic() - 1)


class TestHappyPath:
    def test_a_matching_sse_reply_returns_within_bounds(self):
        resp = _FakeResp(
            ctype="text/event-stream",
            lines=['data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}', ""],
        )
        assert m._parse_http_reply(resp, 1, "srv")["result"] == {"ok": True}

    def test_a_plain_json_reply_returns_within_bounds(self):
        resp = _FakeResp(ctype="application/json", body=b'{"jsonrpc":"2.0","id":1,"result":{}}')
        assert m._parse_http_reply(resp, 1, "srv")["id"] == 1
