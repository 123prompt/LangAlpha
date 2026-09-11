"""Read-side half of multimodal support: what the target model can still see.

An image or PDF block is written into graph state once, by the tool call that
produced it, and then replayed on every later turn. The model reading it is not
the one that wrote it — a user can switch mid-thread, a resilience fallback can
land on a text-only candidate, and a subagent runs its own model entirely — so
whether a block is legal is a question only this side can answer, on the call
where the target is actually known.

Split from ``multimodal`` because the two halves share no state and not every
agent wires both: the Flash assistant has no filesystem tool to intercept, but
still inherits history full of blocks it may not be able to read.
"""

from typing import Any

from langchain.agents.middleware import AgentMiddleware

from ptc_agent.agent.middleware._message_utils import order_tool_results_first
from ptc_agent.agent.middleware._utils import append_to_system_message
from src.llms.attachment_payload import FILE_BLOCK_TYPES, IMAGE_BLOCK_TYPES, block_type
from src.llms.llm import get_input_modalities, get_max_pdf_pages


def _placeholder_for(
    block: Any,
    has_image: bool,
    has_pdf: bool,
    max_pdf_pages: int | None,
) -> dict[str, str] | None:
    """The text block replacing one this target can't read, or None to keep it."""
    kind = block_type(block)

    # Matched on the type alone: this is the only thing standing between a
    # mid-thread switch and a 400, and failing closed costs one placeholder
    # where guessing from the payload keys costs the turn.
    if kind in IMAGE_BLOCK_TYPES and not has_image:
        return {
            "type": "text",
            "text": "[Image attached in a prior turn, not visible to the current model]",
        }

    if kind not in FILE_BLOCK_TYPES:
        return None

    # Same reason as "image" above: `pdf` is the only file-modality flag the
    # manifest carries, so a file block we can't classify — mime absent, null,
    # or non-PDF — is one a model without it has no way to accept.
    if not has_pdf:
        return {
            "type": "text",
            "text": "[PDF attached in a prior turn, not visible to the current model]",
        }

    # Over the target's page ceiling. Unstamped blocks pass: they predate the
    # stamp, and re-deriving the count would mean decoding every PDF in history
    # on every call. They keep the old failure mode; nothing that already worked
    # starts failing here.
    pages = block.get("pages")
    if max_pdf_pages is not None and isinstance(pages, int) and pages > max_pdf_pages:
        return {
            "type": "text",
            "text": (
                f"[PDF attached in a prior turn, {pages} pages, over the current "
                f"model's {max_pdf_pages}-page limit]"
            ),
        }
    return None


def strip_unsupported_content_blocks(
    messages: list,
    has_image: bool,
    has_pdf: bool,
    max_pdf_pages: int | None = None,
) -> list:
    """Replace image/file blocks this target can't read with text placeholders.

    ``max_pdf_pages`` is the target's own page ceiling (None = unbounded). A PDF
    over it is dropped here rather than refused at injection, because the ceiling
    is a property of the model that reads the block, not of the tool call that
    created it — the same document is fine on a 1M-context route and rejected on
    a 200K one, and only this side knows which one is being called.

    Returns the original list when nothing changed, and never mutates a message:
    these are checkpoint objects shared with the rest of the graph.
    """
    result: list = []
    modified = False

    for msg in messages:
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            result.append(msg)
            continue

        new_blocks = []
        changed = False
        for block in content:
            placeholder = _placeholder_for(block, has_image, has_pdf, max_pdf_pages)
            changed |= placeholder is not None
            new_blocks.append(block if placeholder is None else placeholder)

        if not changed:
            result.append(msg)
            continue
        modified = True
        result.append(msg.model_copy(update={"content": new_blocks}))

    return result if modified else messages


def _unsupported_note(can_extract: bool) -> str:
    """The reminder appended to the system prompt on a call where a strip fired.

    One note for both strip reasons: the placeholder already states which one
    applied, next to the block it replaced, so branching the reminder would only
    duplicate that where the model is less likely to be reading.

    ``can_extract`` says whether the agent has a workspace to work the file in.
    The Flash assistant has no filesystem or code tool at all, so telling it to
    extract the file itself would send it looking for a tool it never had.
    """
    extract = (
        "The file is still in the workspace, so prefer extracting it yourself rather "
        "than giving up on it: pull a PDF's text or render its pages to images with "
        "Python in the sandbox, and run OCR or image analysis the same way. "
        if can_extract
        else ""
    )
    return (
        "\n\n<system-reminder>"
        "Content shown as a bracketed placeholder is not visible to the current model; "
        "the placeholder states why. "
        f"{extract}"
        "When that cannot answer the question, be transparent with the user about the "
        "limitation: an unsupported input type means switching to a model that accepts "
        "it, and a page-limit rejection means splitting the document or moving to a "
        "model with a longer context. Work in best effort to answer their query."
        "</system-reminder>"
    )


class MultimodalStripMiddleware(AgentMiddleware):
    """Remove attachment blocks the model about to be called cannot read.

    Wired inside the resilience stack so it judges the post-fallback client, and
    shared across the main and subagent stacks: the target is read off each
    request, so one instance serves every model in the graph.
    """

    def __init__(
        self,
        *,
        model_name: str | None = None,
        custom_modalities: list[str] | None = None,
        can_extract: bool = False,
    ) -> None:
        """
        Args:
            model_name: Configured manifest model, used only to decide whether
                ``custom_modalities`` applies to the target in hand.
            custom_modalities: Per-model modality override from user preferences.
            can_extract: Whether this agent has a workspace it could extract an
                unreadable file in, which changes the advice in the reminder.
        """
        super().__init__()
        self.model_name = model_name
        self.custom_modalities = custom_modalities
        self.unsupported_note = _unsupported_note(can_extract)

    def _resolve_target(self, request: Any) -> tuple[str | None, list[str]]:
        """Name and modalities of the model this request will actually reach.

        Read off ``request.model`` rather than the configured name so a
        resilience fallback — or a subagent running its own model — is judged on
        the client in hand. The name comes back too because the PDF page ceiling
        is per-model and has to be looked up against the same target.
        """
        metadata = getattr(request.model, "metadata", None)
        stamped = metadata.get("manifest_model") if isinstance(metadata, dict) else None

        if not isinstance(stamped, str) or not stamped:
            # Unattributable: the client skipped LLM.get_llm(), which today means
            # a subagent whose config names a bare model string for deepagents to
            # resolve via init_chat_model. Falling back to the configured name
            # would lend a vision parent's modalities to a text-only target and
            # replay exactly the blocks this strip exists to remove, so an
            # unstamped client is judged text-only — the same fail-closed reading
            # LLM.get_llm() documents when it writes the stamp.
            return None, ["text"]
        # ``custom_modalities`` is a per-model override from user preferences, so
        # it describes only the model it was configured for — a fallback is a
        # different model and has to be judged on its own manifest entry.
        overrides = self.custom_modalities if stamped == self.model_name else None
        return stamped, get_input_modalities(stamped, custom_modalities=overrides)

    async def awrap_model_call(self, request, handler):
        # Runs for every target, not just modality-limited ones: the interleaving
        # only arises where injection succeeded, which is a model that can see it.
        messages = order_tool_results_first(request.messages)
        overrides: dict[str, Any] = {}

        stamped, modalities = self._resolve_target(request)
        has_image = "image" in modalities
        has_pdf = "pdf" in modalities
        # Looked up even for a fully multimodal target: a model that accepts PDFs
        # can still be handed one past its page ceiling, which is the judgement
        # the tool side deliberately no longer attempts.
        max_pdf_pages = get_max_pdf_pages(stamped) if stamped and has_pdf else None

        sanitized = strip_unsupported_content_blocks(
            messages, has_image, has_pdf, max_pdf_pages
        )
        if sanitized is not messages:
            messages = sanitized
            # Told to the model that actually can't see the file, on the call
            # where that is true. The tool-time note this replaces judged the
            # configured model and froze that verdict into the transcript,
            # which a subagent on its own model then inherited wrongly.
            overrides["system_message"] = append_to_system_message(
                request.system_message, self.unsupported_note
            )

        if messages is not request.messages:
            overrides["messages"] = messages
        return await handler(request.override(**overrides) if overrides else request)


__all__ = ["MultimodalStripMiddleware", "strip_unsupported_content_blocks"]
