"""Turning a turn's attachments into something the model can act on.

Every file the user attached takes one of three routes, decided by what this
turn's model accepts: a native content block, a sandbox path the agent opens
with Python, or a note saying the file exists and cannot be read. Which routes
are available is the difference between the two flavors, so they get a call
each rather than one function with a mode flag: PTC has a sandbox to upload to
and can name a path per file, Flash has neither and can only say what arrived.
"""

from __future__ import annotations

from typing import Any

from ptc_agent.core.project_context import ProjectContext

from src.llms.llm import get_input_modalities
from src.server.models.chat import ChatRequest
from src.server.utils.multimodal_context import (
    build_file_reminder,
    build_unsupported_reminder,
    filter_multimodal_by_capability,
    inject_multimodal_context,
    parse_multimodal_contexts,
    upload_to_sandbox,
)

from .request_prep import _append_to_last_user_message, logger


def _modalities(effective_model: str | None, config: Any) -> list[str]:
    """What this turn's model accepts natively, text alone when it is unresolved."""
    return (
        get_input_modalities(
            effective_model, custom_modalities=config.input_modalities
        )
        if effective_model
        else ["text"]
    )


async def attach_request_files(
    messages: list[dict],
    request: ChatRequest,
    session: Any | None,
    effective_model: str | None,
    config: Any,
    *,
    project: ProjectContext | None = None,
) -> list[dict]:
    """Upload the turn's attachments and describe them to the PTC agent.

    All attachments are uploaded to sandbox (when available) so the agent always
    has file access. Model-supported modalities also get native content blocks
    merged into the user message.
    """
    multimodal_contexts = parse_multimodal_contexts(request.additional_context)
    if not multimodal_contexts or request.hitl_response:
        return messages

    # 1. Upload ALL files to sandbox
    file_paths: list = []
    if session and session.sandbox:
        file_paths = await upload_to_sandbox(
            multimodal_contexts, session.sandbox, project=project
        )
        logger.info(
            f"[PTC_CHAT] Uploaded {len(multimodal_contexts)} attachment(s) to sandbox"
        )

    # 2. Filter by model capability for native content blocks
    modalities = _modalities(effective_model, config)
    supported, unsupported, file_only = filter_multimodal_by_capability(
        multimodal_contexts, modalities
    )

    # 3. Inject supported as native content blocks (merged into user message)
    if supported:
        supported_paths = [
            file_paths[i]
            for i, ctx in enumerate(multimodal_contexts)
            if ctx in supported
        ] if file_paths else None
        messages = inject_multimodal_context(
            messages, supported, file_paths=supported_paths
        )
        logger.info(
            f"[PTC_CHAT] Multimodal context injected: "
            f"{len(supported)} supported attachment(s)"
        )

    # Helper to build per-file path notes
    def _file_note(ctx, idx):
        desc = ctx.description or "file"
        data = ctx.data
        mime = data.split(":")[1].split(";")[0] if ":" in data else "unknown"
        fpath = file_paths[idx] if file_paths and idx < len(file_paths) else None
        if fpath:
            return (
                f"The user attached a file ({desc}, {mime}). "
                f"It has been saved to {fpath}. "
                f"Use Python to process it."
            )
        return f"The user attached a file ({desc}, {mime})."

    # 4. Unsupported image/PDF: "cannot view" warning + file paths
    if unsupported:
        notes = [
            _file_note(ctx, i)
            for i, ctx in enumerate(multimodal_contexts)
            if ctx in unsupported
        ]
        _append_to_last_user_message(messages, build_unsupported_reminder(notes))

    # 5. File-only (xlsx, csv, etc.): path notes only, no "cannot view"
    if file_only:
        notes = [
            _file_note(ctx, i)
            for i, ctx in enumerate(multimodal_contexts)
            if ctx in file_only
        ]
        _append_to_last_user_message(messages, build_file_reminder(notes))
        logger.info(
            f"[PTC_CHAT] {len(file_only)} file-only attachment(s) "
            f"uploaded to sandbox for {effective_model}"
        )

    return messages


def attach_flash_request_files(
    messages: list[dict],
    request: ChatRequest,
    effective_model: str | None,
    config: Any,
) -> list[dict]:
    """Describe the turn's attachments to the Flash agent.

    Supported items are injected as native content blocks; unsupported ones get
    a text note naming only the type, because with no sandbox there is no path
    to hand over and nothing for the agent to do with a file it cannot read.
    """
    multimodal_contexts = parse_multimodal_contexts(request.additional_context)
    if not multimodal_contexts:
        return messages

    modalities = _modalities(effective_model, config)
    supported, unsupported, file_only = filter_multimodal_by_capability(
        multimodal_contexts, modalities
    )
    if file_only:
        logger.warning(
            f"[FLASH_CHAT] {len(file_only)} file-only attachment(s) "
            f"ignored (Flash mode has no sandbox)"
        )
    if supported:
        messages = inject_multimodal_context(messages, supported)
        logger.info(
            f"[FLASH_CHAT] Multimodal context injected: "
            f"{len(supported)} supported attachment(s)"
        )
    if unsupported:
        types = list(
            set(
                "PDF"
                if (c.data if hasattr(c, "data") else "").startswith(
                    "data:application/pdf"
                )
                else "image"
                for c in unsupported
            )
        )
        _append_to_last_user_message(
            messages,
            build_unsupported_reminder(
                [f"The user attached {', '.join(types)} file(s)."]
            ),
        )
        logger.info(
            f"[FLASH_CHAT] {len(unsupported)} unsupported attachment(s) "
            f"noted for {effective_model}"
        )

    return messages
