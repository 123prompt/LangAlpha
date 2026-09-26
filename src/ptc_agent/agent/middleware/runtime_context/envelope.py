"""The one block that is true for a single call, rendered into the tail.

The envelope has shrunk to what genuinely changes between the calls of one turn:
today that is market_watch's live stamp and nothing else. Everything frozen for
the turn is a durable row written once into history, where its place in time is
visible, so a call that refreshed nothing renders nothing here.

One block, one budget. The hard cap drops it rather than trimming it, because a
half-rendered stamp is worse for the model than no stamp at all.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ptc_agent.agent.middleware.runtime_context import clock
from ptc_agent.agent.middleware.runtime_context.durable import DurableUpdate
from ptc_agent.agent.middleware.runtime_context.templates import render_template

logger = logging.getLogger(__name__)

ENVELOPE_OPEN = "<system-reminder>"
ENVELOPE_CLOSE = "</system-reminder>"

# Steady target is what the tail should cost on a normal call; the hard cap is
# what it may never exceed. Only the hard cap drops the block, because a tail a
# little over the target is worth more than a call with no runtime context.
ENVELOPE_STEADY_TOKENS = 1500
ENVELOPE_HARD_CAP_TOKENS = 2500


def count_tokens(text: str) -> int:
    """Token count for budget decisions; falls back to a chars/4 estimate.

    tiktoken is not the tokenizer of every provider we route to, so this is an
    approximation either way. It is used only to decide whether to drop the
    block, and the cheaper estimate is close enough for that.
    """
    if not text:
        return 0
    try:
        from ptc_agent.agent.middleware.compaction.utils import _get_tiktoken_encoder

        return len(_get_tiktoken_encoder().encode(text))
    except Exception:  # noqa: BLE001 - budgeting must never break a turn
        return len(text) // 4


def render_header(guidance: str) -> str:
    return render_template("envelope/header.md.j2", guidance=guidance)


def render_call_updates(
    updates: list[DurableUpdate], now: datetime, guidance: str
) -> str:
    """The tail block for the rows this call contributed, or "" when there are none.

    Newest last, because the model reads the block top to bottom and the most
    recent state should be the last thing it sees on the subject. Nothing here
    is persisted: a row worth keeping belongs in history, where its place in
    time is visible.
    """
    rows = sorted((u for u in updates if u.text), key=lambda u: u.created_at)
    if not rows:
        return ""
    body = render_template(
        "envelope/updates.md.j2",
        updates=[_update_item(row, now) for row in rows],
        guidance=guidance,
    )
    if not body:
        return ""

    block = f"{render_header(guidance)}\n\n{body}"
    total = count_tokens(f"{ENVELOPE_OPEN}\n{block}\n{ENVELOPE_CLOSE}")
    if total > ENVELOPE_HARD_CAP_TOKENS:
        logger.warning(
            "[Envelope] tail is %d tokens, over the %d cap; dropped the call updates",
            total,
            ENVELOPE_HARD_CAP_TOKENS,
        )
        return ""
    if total > ENVELOPE_STEADY_TOKENS:
        logger.debug(
            "[Envelope] tail is %d tokens, over the %d steady target",
            total,
            ENVELOPE_STEADY_TOKENS,
        )
    return block


def frame_reminder(text: str) -> str:
    """*text* inside the ``<system-reminder>`` wrapper.

    Framing belongs to the carrier, not the renderer: the wrapper is what marks
    harness text inside a user-role message, and an operator role carries that
    meaning itself, so the same text goes out bare on those channels. The budget
    still counts the wrapper because the reminder shape is the largest form the
    block can take.
    """
    return f"{ENVELOPE_OPEN}\n{text}\n{ENVELOPE_CLOSE}"


def _update_item(update: DurableUpdate, now: datetime) -> dict[str, Any]:
    """Render shape for one row: the stored dict with an age instead of a stamp.

    An absolute timestamp makes the model do the subtraction; a per-call row's
    whole subject is how fresh it is, so the envelope hands over the answer.
    """
    item = update.to_dict()
    item["created_at"] = f"{clock.format_elapsed(update.created_at, now)} ago"
    return item
