"""The write side: getting an image or PDF onto the tool result that produced it.

Its counterpart, the strip that decides what the model about to be called can
still read, is covered in ``test_multimodal_strip``.
"""

import io
import types

import httpx
import pypdf
import pytest
from langchain_core.messages import ToolMessage
from PIL import Image

from ptc_agent.agent.middleware.file_operations import multimodal
from ptc_agent.agent.middleware.file_operations.multimodal import (
    MultimodalMiddleware,
    _is_visual_request,
    attach_to_tool_result,
)


def _pdf_bytes(pages: int = 1) -> bytes:
    """A structurally valid PDF. Generated rather than committed as a blob so the
    page count is a parameter — that is what the provider ceiling is measured in."""
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _ack() -> ToolMessage:
    """The Read tool's own result, which is what the handlers are handed.

    Its text is what the attachment is merged onto, so it has to be a
    ToolMessage here: an AIMessage stand-in takes the construct-a-new-one
    branch and never exercises the copy the live path takes."""
    return ToolMessage(content="ok", tool_call_id="call-1")


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(buf, format="PNG")
    return buf.getvalue()


class _Sandbox:
    """Stands in for PTCSandbox — which normalizes on its own, not on download."""

    def __init__(self, content: bytes = b"", *, work_dir: str = "/home/workspace"):
        self._content = content
        self._work_dir = work_dir
        self.downloaded: str | None = None

    def normalize_path(self, path: str) -> str:
        if path.startswith((self._work_dir, "/tmp")):
            return path
        return f"{self._work_dir}/{path.lstrip('/')}"

    async def adownload_file_bytes(self, path: str) -> bytes:
        self.downloaded = path
        return self._content


class TestVisualRequestRouting:
    def test_memo_pdf_is_not_intercepted(self):
        """Read serves memo PDFs as extracted text; they have no sandbox-FS copy,
        so intercepting would swap real content for a not-found error."""
        assert not _is_visual_request(".agents/user/memo/report.pdf")
        assert not _is_visual_request("/.agents/user/memo/report.pdf")
        assert not _is_visual_request("./.agents/user/memo/report.pdf")

    def test_workspace_pdf_is_intercepted(self):
        assert _is_visual_request("results/report.pdf")

    def test_tool_name_matches_the_registered_read_tool(self):
        """A drift here silently disables the whole injection half — it fails by
        matching nothing, not by raising, which is how it went unnoticed before."""
        from ptc_agent.agent.tools.file_ops import create_filesystem_tools

        # The factory only closes over the backend; nothing touches it until a
        # tool is actually invoked, so a bare stub is enough to read the names.
        names = {t.name for t in create_filesystem_tools(types.SimpleNamespace())}
        assert MultimodalMiddleware.TOOL_NAME in names


class TestTheWriteSideNeverJudgesTheModel:
    """Which model can see the file is decided where the model is known.

    One middleware instance is shared with every subagent, so the configured name
    is not the consuming model. Deciding at tool-call time withheld the block from
    a subagent running its own vision model — and the read side only ever removes
    blocks, so nothing downstream could recover a block never created.
    """

    @staticmethod
    def _read(file_path):
        return types.SimpleNamespace(
            tool_call={"name": "Read", "args": {"file_path": file_path}, "id": "tc-1"}
        )

    @staticmethod
    async def _handler(_request):
        return ToolMessage(content="ok", tool_call_id="tc-1")

    @pytest.mark.asyncio
    async def test_a_text_only_configured_model_still_injects(self):
        """The block has to exist for a vision subagent sharing this instance."""
        mw = MultimodalMiddleware(sandbox=_Sandbox(_png_bytes()))

        result = await mw.awrap_tool_call(self._read("chart.png"), self._handler)
        assert isinstance(result, ToolMessage)
        assert result.content[-1]["type"] == "image_url"

    @pytest.mark.parametrize(
        "file_path",
        ["chart.png", "report.pdf", "https://example.com/asset"],
        ids=["image-ext", "pdf-ext", "url-without-extension"],
    )
    @pytest.mark.asyncio
    async def test_no_verdict_is_frozen_into_the_transcript(
        self, file_path, monkeypatch
    ):
        """The extensionless URL is the interesting one: it could resolve to
        either kind, and the old gate judged all three off the configured name.

        The transport is mocked because the assertion is a negative: an
        unmocked URL case would reach the network, and every way that can fail
        also satisfies "no refusal in the content", so the case would pass
        without ever exercising the path it names.
        """
        served = _png_bytes()

        def handler(request):
            return httpx.Response(
                200, content=served, headers={"content-type": "image/png"}
            )

        monkeypatch.setattr(
            multimodal, "GuardedAsyncTransport", lambda: httpx.MockTransport(handler)
        )
        mw = MultimodalMiddleware(sandbox=_Sandbox(served))
        result = await mw.awrap_tool_call(self._read(file_path), self._handler)
        assert "does not support" not in str(result.content)
        # Non-vacuous: the path really ran and produced the block.
        assert result.content[-1]["type"] == "image_url"



class TestContentSizeCap:
    @pytest.mark.asyncio
    async def test_oversized_download_is_aborted(self, monkeypatch):
        """The body is streamed so the cap can fire mid-transfer — a Content-Length
        check alone is a header a server can simply lie about."""
        mw = MultimodalMiddleware(sandbox=_Sandbox())
        monkeypatch.setattr(mw, "MAX_CONTENT_BYTES", 1024)

        def handler(request):
            return httpx.Response(200, content=b"x" * 4096)

        monkeypatch.setattr(
            multimodal, "GuardedAsyncTransport", lambda: httpx.MockTransport(handler)
        )

        result = await mw._handle_url_content(
            "https://example.com/big.png", _ack(), "call-1"
        )
        assert "larger than" in result.content
        assert result.tool_call_id == "call-1"

    @pytest.mark.asyncio
    async def test_oversized_sandbox_file_is_refused(self, monkeypatch):
        """The sandbox path was uncapped while the URL path was not. Both write
        into graph state, so an oversized block there is checkpointed and
        replayed on every later turn — a permanent 400, not a one-turn error."""
        mw = MultimodalMiddleware(sandbox=_Sandbox(b"x" * 4096))
        monkeypatch.setattr(mw, "MAX_CONTENT_BYTES", 1024)

        result = await mw._handle_sandbox_content(
            "/home/workspace/big.png", _ack(), "call-1"
        )
        assert "larger than" in result.content
        assert result.tool_call_id == "call-1"

    @pytest.mark.asyncio
    async def test_a_refusal_never_reaches_graph_state(self, monkeypatch):
        """Attaching the block would write it into the checkpoint; refusing has
        to leave the result plain text or the cap accomplishes nothing."""
        mw = MultimodalMiddleware(sandbox=_Sandbox(b"x" * 4096))
        monkeypatch.setattr(mw, "MAX_CONTENT_BYTES", 1024)

        result = await mw._handle_sandbox_content(
            "/home/workspace/big.png", _ack(), "call-1"
        )
        assert isinstance(result, ToolMessage)
        assert isinstance(result.content, str)

    @pytest.mark.asyncio
    async def test_a_blocked_address_does_not_name_the_resolved_ip(self, monkeypatch):
        """The URL is model-chosen, so echoing the guard's message back — it names
        the resolved address — turns Read into a DNS-to-IP probe steerable by
        anything the agent reads."""
        from src.tools.web.inhouse.guard import BlockedAddressError

        class _Blocking(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                raise BlockedAddressError(
                    "Blocked private/reserved address 10.11.12.13 for host 'x.corp'",
                    request=request,
                )

        mw = MultimodalMiddleware(sandbox=_Sandbox())
        monkeypatch.setattr(multimodal, "GuardedAsyncTransport", _Blocking)

        result = await mw._handle_url_content(
            "http://x.corp/a.png", _ack(), "call-1"
        )
        assert "10.11.12.13" not in result.content
        assert "x.corp" in result.content  # the URL the model already knows
        assert "not allowed" in result.content

    def test_the_cap_matches_the_binding_provider_limit(self):
        """5MB is Anthropic's per-image ceiling, which binds before any
        per-request limit. Raising this re-opens the checkpoint brick."""
        assert MultimodalMiddleware.MAX_CONTENT_BYTES == 5 * 1024 * 1024


class TestSandboxPathNormalization:
    @pytest.mark.asyncio
    async def test_a_virtual_path_is_normalized_before_download(self):
        """The Read tool normalizes through SandboxBackend, this middleware holds
        the sandbox itself. Skipping it makes the middleware miss a file the tool
        just read and overwrite that success with a not-found error."""
        sandbox = _Sandbox(_png_bytes())
        mw = MultimodalMiddleware(sandbox=sandbox)

        result = await mw._handle_sandbox_content(
            "/results/chart.png", _ack(), "call-1"
        )
        assert sandbox.downloaded == "/home/workspace/results/chart.png"
        assert isinstance(result, ToolMessage)

    @pytest.mark.asyncio
    async def test_an_already_absolute_path_is_left_alone(self):
        sandbox = _Sandbox(_png_bytes())
        mw = MultimodalMiddleware(sandbox=sandbox)

        await mw._handle_sandbox_content(
            "/home/workspace/work/t/charts/fig.png", _ack(), "call-1"
        )
        assert sandbox.downloaded == "/home/workspace/work/t/charts/fig.png"


class TestContentIsTypedByItsBytes:
    """The extension is whatever wrote the file claimed, and a URL may carry none
    at all. Both injection paths checkpoint what they build, so a wrong call here
    is replayed on every later turn instead of failing once.
    """

    @pytest.mark.asyncio
    async def test_a_corrupt_sandbox_image_is_refused(self):
        """The URL path already ran PIL over its bytes; the sandbox path trusted
        the extension, so a truncated chart reached graph state unexamined."""
        mw = MultimodalMiddleware(sandbox=_Sandbox(_png_bytes()[:20]))

        result = await mw._handle_sandbox_content(
            "chart.png", _ack(), "call-1"
        )
        assert isinstance(result, ToolMessage)
        assert isinstance(result.content, str)
        assert "not a readable image or PDF" in result.content

    @pytest.mark.asyncio
    async def test_a_sandbox_pdf_named_png_is_read_as_a_pdf(self):
        mw = MultimodalMiddleware(sandbox=_Sandbox(_pdf_bytes()))

        result = await mw._handle_sandbox_content(
            "report.png", _ack(), "call-1"
        )
        blocks = result.content
        assert [b["type"] for b in blocks] == ["text", "file"]
        assert blocks[-1]["mime_type"] == "application/pdf"

    @pytest.mark.asyncio
    async def test_an_extensionless_url_serving_a_pdf_is_not_judged_as_an_image(
        self, monkeypatch
    ):
        """Signed and redirected URLs routinely end without a suffix; branching on
        it sent every one of them down the image path."""
        mw = MultimodalMiddleware(sandbox=_Sandbox())
        monkeypatch.setattr(
            multimodal,
            "GuardedAsyncTransport",
            lambda: httpx.MockTransport(
                lambda request: httpx.Response(200, content=_pdf_bytes())
            ),
        )

        result = await mw._handle_url_content(
            "https://files.example.com/d/abc123", _ack(), "call-1"
        )
        assert isinstance(result, ToolMessage)
        assert result.content[-1]["mime_type"] == "application/pdf"

    @pytest.mark.asyncio
    async def test_a_format_no_provider_accepts_is_refused_not_relabeled(
        self, monkeypatch
    ):
        """The old URL path defaulted an unmapped PIL format to image/png, which
        ships a BMP under a PNG mime and earns a 400 on every replay."""
        buf = io.BytesIO()
        Image.new("RGB", (2, 2), "blue").save(buf, format="BMP")
        mw = MultimodalMiddleware(sandbox=_Sandbox())
        monkeypatch.setattr(
            multimodal,
            "GuardedAsyncTransport",
            lambda: httpx.MockTransport(
                lambda request: httpx.Response(200, content=buf.getvalue())
            ),
        )

        result = await mw._handle_url_content(
            "https://example.com/a.bmp", _ack(), "call-1"
        )
        assert isinstance(result, ToolMessage)
        assert isinstance(result.content, str)
        assert "BMP" in result.content


class TestPDFsAreVerifiedNotSniffed:
    """A `%PDF` prefix is four bytes of claim. Everything past it — the xref, the
    page tree, the page count — is what a provider actually validates, and the
    tool result carrying the block is written to the checkpoint before any
    provider sees it.
    """

    @pytest.mark.asyncio
    async def test_a_header_only_pdf_never_reaches_graph_state(self):
        """The bytes that pass a prefix check and nothing else."""
        mw = MultimodalMiddleware(sandbox=_Sandbox(b"%PDF-1.4 body"))

        result = await mw._handle_sandbox_content(
            "report.pdf", _ack(), "call-1"
        )
        assert isinstance(result, ToolMessage)
        assert isinstance(result.content, str)
        assert "not a readable PDF" in result.content

    @pytest.mark.asyncio
    async def test_a_truncated_pdf_is_refused(self):
        """The realistic shape: an interrupted download or a half-written report,
        whose head is a genuine PDF."""
        whole = _pdf_bytes()
        mw = MultimodalMiddleware(sandbox=_Sandbox(whole[: len(whole) * 6 // 10]))

        result = await mw._handle_sandbox_content(
            "report.pdf", _ack(), "call-1"
        )
        assert isinstance(result, ToolMessage)
        assert "not a readable PDF" in result.content

    @pytest.mark.asyncio
    async def test_a_pdf_over_the_page_ceiling_is_refused_with_its_count(
        self, monkeypatch
    ):
        """Page count is the ceiling that binds: this fixture is a few KB, so no
        byte cap would catch it. The message names the count so the agent can
        split the document rather than retry the same read. The cap is patched
        down because what is under test is that it is enforced, not its value."""
        monkeypatch.setattr(multimodal, "MAX_PDF_PAGES", 3)
        mw = MultimodalMiddleware(sandbox=_Sandbox(_pdf_bytes(4)))

        result = await mw._handle_sandbox_content(
            "long.pdf", _ack(), "call-1"
        )
        assert isinstance(result, ToolMessage)
        assert isinstance(result.content, str)
        assert "4-page" in result.content

    @pytest.mark.asyncio
    async def test_a_pdf_at_the_page_ceiling_is_accepted(self, monkeypatch):
        """The limit is inclusive — off-by-one here silently rejects a legal doc."""
        monkeypatch.setattr(multimodal, "MAX_PDF_PAGES", 3)
        mw = MultimodalMiddleware(sandbox=_Sandbox(_pdf_bytes(3)))

        result = await mw._handle_sandbox_content(
            "long.pdf", _ack(), "call-1"
        )
        assert isinstance(result, ToolMessage)
        assert result.content[-1]["mime_type"] == "application/pdf"

    @pytest.mark.asyncio
    async def test_the_injection_cap_is_the_widest_ceiling_not_the_tightest(self):
        """The case that motivated splitting the cap in two: a 150-page filing is
        legal on a 1M-context route, and injection cannot know it isn't headed
        there, so refusing it at tool time would be a guess against the user."""
        mw = MultimodalMiddleware(sandbox=_Sandbox(_pdf_bytes(150)))

        result = await mw._handle_sandbox_content(
            "filing.pdf", _ack(), "call-1"
        )
        assert isinstance(result, ToolMessage)
        assert result.content[-1]["pages"] == 150

    @pytest.mark.asyncio
    async def test_trailing_bytes_do_not_condemn_an_otherwise_readable_pdf(self):
        """Verification has to reject damage, not tidiness. Real PDFs routinely
        carry junk after the final %%EOF, and providers accept them."""
        mw = MultimodalMiddleware(sandbox=_Sandbox(_pdf_bytes() + b"\n<!-- appended -->"))

        result = await mw._handle_sandbox_content(
            "report.pdf", _ack(), "call-1"
        )
        assert isinstance(result, ToolMessage)
        assert result.content[-1]["mime_type"] == "application/pdf"


class TestTheAttachmentRidesOnTheToolResult:
    """The attachment is the tool result, not a message that follows it.

    A second message could be separated from the call it answers by a
    compaction cutoff, by a parallel batch's ordering, or by any later history
    surgery. Carrying the blocks on the result makes those failures
    unrepresentable: the attachment moves wherever the result moves.
    """

    _BLOCKS = [
        {"type": "text", "text": "[Viewing image]"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
    ]

    def test_the_acknowledgment_merges_into_the_leading_text_block(self):
        result = attach_to_tool_result(
            ToolMessage(content="Loading image: chart.png", tool_call_id="call-1"),
            self._BLOCKS,
        )
        assert result.content[0] == {
            "type": "text",
            "text": "Loading image: chart.png\n[Viewing image]",
        }
        assert result.content[1] is self._BLOCKS[1]

    def test_the_result_keeps_its_own_identity(self):
        """Copied rather than rebuilt: name and tool_call_id are what pair the
        result with its call."""
        result = attach_to_tool_result(
            ToolMessage(content="ok", tool_call_id="call-1", name="Read"),
            self._BLOCKS,
        )
        assert result.tool_call_id == "call-1"
        assert result.name == "Read"

    def test_an_acknowledgment_with_no_text_block_to_merge_into_leads_instead(self):
        result = attach_to_tool_result(
            ToolMessage(content="ok", tool_call_id="call-1"),
            [self._BLOCKS[1]],
        )
        assert [b["type"] for b in result.content] == ["text", "image_url"]
        assert result.content[0]["text"] == "ok"

    def test_the_blocks_handed_in_are_not_mutated(self):
        """The caller built them from the file bytes; a later read reuses the
        same list."""
        blocks = [dict(b) for b in self._BLOCKS]
        before = [dict(b) for b in blocks]
        attach_to_tool_result(
            ToolMessage(content="ok", tool_call_id="call-1"), blocks
        )
        assert blocks == before
