"""The tail middleware: the carrier for persisted rows, plus one live block.

Two jobs, and the first is the larger one. Every durable row in the request is
history written in a provider-neutral form, so this middleware asks the carrier
to compose the request in the shape the model in hand reads, and takes back the
site breakpoint 4 may go on: the newest content the next call still carries.

The second is the envelope itself, which has shrunk to what genuinely changes
between the calls of one turn: today that is market_watch and nothing else.
Everything frozen for the turn is a row written once at the turn boundary
(``turn.py``, ``durable.py``), so a call that refreshed nothing adds nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse

from ptc_agent.agent.middleware.provider_cache import breakpoint_marker
from ptc_agent.agent.middleware.runtime_context.carrier import (
    apply_breakpoint,
    compose_request,
    operator_window,
    resolve_carrier_shape,
)
from ptc_agent.agent.middleware.runtime_context.durable import (
    DurableUpdate,
    is_runtime_update_message,
)
from ptc_agent.agent.middleware.runtime_context.envelope import render_call_updates

logger = logging.getLogger(__name__)

# Rows contributed for one model call only, put on the request by a middleware
# outside this one (``market_watch``) through ``request.override``. Not a state
# channel and never written back: named after the call so nobody mistakes it
# for one.
REQUEST_CALL_UPDATES = "runtime_call_updates"


class TailEnvelopeMiddleware(AgentMiddleware):
    """Carries the durable rows in this model's shape, and the envelope last.

    Must sit innermost (just before the reasoning sanitizer) so the envelope is
    the true tail of the request and the content in front of it is the last of
    the durable history, which is where breakpoint 4 goes.

    Args:
        now: Request time, frozen at agent build like ``current_time`` is.
            Read only for the age a per-call row is rendered with.
        guidance: Resolved prompt guidance level. None resolves it lazily from
            ``model_name``.
        model_name: Model the turn resolved to, used only for guidance.
    """

    def __init__(
        self,
        *,
        now: datetime | None = None,
        guidance: str | None = None,
        model_name: str | None = None,
    ) -> None:
        super().__init__()
        self._now = now or datetime.now(tz=UTC)
        self._guidance_value = guidance
        self._model_name = model_name

    # -- rendering inputs ---------------------------------------------------

    def _guidance(self) -> str:
        if self._guidance_value is None:
            from ptc_agent.agent.prompts import resolve_prompt_guidance

            self._guidance_value = resolve_prompt_guidance(self._model_name)
        return self._guidance_value

    def _call_updates(self, request: ModelRequest) -> list[DurableUpdate]:
        state = getattr(request, "state", None) or {}
        rows: list[DurableUpdate] = []
        for row in state.get(REQUEST_CALL_UPDATES) or []:
            if isinstance(row, DurableUpdate):
                rows.append(row)
            elif isinstance(row, dict):
                rows.append(DurableUpdate.from_dict(row))
        return rows

    def _render(self, request: ModelRequest) -> str:
        try:
            return render_call_updates(
                self._call_updates(request), self._now, self._guidance()
            )
        except Exception:  # noqa: BLE001 - context is never worth failing a turn for
            logger.warning("[Envelope] render failed; sending the call without it", exc_info=True)
            return ""

    # -- middleware hooks ---------------------------------------------------

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        # Sync fallback: the async agent won't call this but the protocol requires it.
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        original = request.messages or []
        envelope = self._render(request)
        if not envelope and not any(is_runtime_update_message(m) for m in original):
            return await handler(request)

        model = getattr(request, "model", None)
        try:
            shape = resolve_carrier_shape(model)
            composed = compose_request(original, envelope, shape)
        except Exception:  # noqa: BLE001 - context is never worth failing a turn for
            logger.warning(
                "[Envelope] composing the request failed; sending it as it arrived",
                exc_info=True,
            )
            return await handler(request)

        marker = breakpoint_marker(model)
        settings = request.model_settings
        if marker is not None and composed.pin is not None:
            apply_breakpoint(composed.messages, composed.pin, *marker)
            settings = _without_top_level_marker(settings, marker[0])
        # The system shape is honored by the Anthropic client only inside this
        # window, so the opt-in is per composed call, never per client.
        with operator_window(shape):
            return await handler(
                request.override(messages=composed.messages, model_settings=settings)
            )


def _without_top_level_marker(settings: dict, key: str) -> dict:
    """The model settings with the provider's request-level breakpoint removed.

    The provider caching middleware also puts the marker on the request itself
    (Anthropic's top-level ``cache_control``), which the direct API turns into
    a breakpoint of its own on the last block. With breakpoint 4 pinned here
    that would be a fifth, and the request is refused outright; the pin is the
    one measured to be read back, so it is the one that stays.
    """
    if key not in (settings or {}):
        return settings
    return {k: v for k, v in settings.items() if k != key}
