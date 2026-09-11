"""Failure ToolMessages out of the Task tool must carry ``status="error"``.

``ToolMessage.status`` defaults to ``"success"`` and now rides the wire, where
the frontend reads it. A refused launch opens no run and no channel, so nothing
will ever arrive to settle its card: an unstamped failure leaves the card
spinning for the life of the thread.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from ptc_agent.agent.middleware.background_subagent import task_actions
from ptc_agent.agent.middleware.background_subagent.registry import (
    BackgroundTaskRegistry,
)
from ptc_agent.agent.middleware.background_subagent.spawn import TaskRunRefused


class _RefusingMiddleware:
    """Duck-typed middleware whose ledger admission always refuses."""

    def __init__(self) -> None:
        self.registry = BackgroundTaskRegistry()
        self.namespace_owner = None
        self._resume_claims: set[str] = set()

    async def admit_task_run(self, task: Any, **kwargs: Any) -> str:
        raise TaskRunRefused("slot busy")


@pytest.mark.asyncio
async def test_a_refused_task_launch_is_stamped_an_error():
    mw = _RefusingMiddleware()

    message = await task_actions._handle_init(
        mw,
        None,
        None,
        subagent_type="research",
        description="d",
        prompt="p",
        tool_call_id="call-1",
        current_run_id="run-1",
    )

    assert message.content.startswith("Error: could not start ")
    assert message.status == "error"


_SWEPT_FILES = ("task_actions.py", "middleware.py")


def _constant_text(node: ast.AST) -> str:
    """The literal text of an expression, ignoring its interpolated holes.

    An f-string's prefix is a Constant like any other, so a content expression
    built out of runtime values still exposes the wording that decides whether
    it reads as a failure.
    """
    return "".join(
        part.value
        for part in ast.walk(node)
        if isinstance(part, ast.Constant) and isinstance(part.value, str)
    )


def _name_bindings(tree: ast.AST) -> dict[str, str]:
    """Every ``name = <text>`` binding in the module, by name.

    Content assembled into a local and then passed as ``content=msg`` is
    invisible to a sweep that only reads the call site, which is how a
    constructed failure escapes. Names are not scoped here: a collision would
    only widen the sweep, and a false candidate is caught by its own status.
    """
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, (ast.NamedExpr,)):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and node.value is not None:
                text = _constant_text(node.value)
                if text:
                    bindings[target.id] = bindings.get(target.id, "") + text
    return bindings


def _reads_as_failure(text: str) -> bool:
    return text.lower().lstrip().startswith("error")


def _error_tool_messages(tree: ast.AST) -> list[ast.Call]:
    bindings = _name_bindings(tree)
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "ToolMessage":
            continue
        for kw in node.keywords:
            if kw.arg != "content":
                continue
            text = _constant_text(kw.value)
            if isinstance(kw.value, ast.Name):
                text = bindings.get(kw.value.id, text)
            if _reads_as_failure(text):
                calls.append(node)
    return calls


def _unstamped(calls: list[ast.Call]) -> list[int]:
    return [
        call.lineno
        for call in calls
        if not any(
            kw.arg == "status"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value == "error"
            for kw in call.keywords
        )
    ]


@pytest.mark.parametrize("filename", _SWEPT_FILES)
def test_every_error_tool_message_stamps_its_status(filename: str):
    path = Path(task_actions.__file__).parent / filename
    tree = ast.parse(path.read_text())
    calls = _error_tool_messages(tree)
    assert calls, f"no Error-prefixed ToolMessage found in {filename}"
    unstamped = _unstamped(calls)
    assert unstamped == [], f"{filename} lines {unstamped} return a failure as success"


def test_the_sweep_sees_a_failure_whose_content_was_built_elsewhere():
    """Guards the sweep itself: content bound to a local used to slip through.

    A failure assembled before the call site is the shape a literal-only sweep
    walks straight past, so the detector is exercised against one directly.
    """
    source = """
def build(task_id, action):
    reason = f"Error: task_id is required for '{action}' action."
    stamped = f"Error: Task-{task_id} not found."
    fine = f"Dispatched Task-{task_id}."
    return [
        ToolMessage(content=reason, tool_call_id="c"),
        ToolMessage(content=stamped, tool_call_id="c", status="error"),
        ToolMessage(content=fine, tool_call_id="c"),
    ]
"""
    calls = _error_tool_messages(ast.parse(source))

    assert len(calls) == 2, "the built failures are what the sweep must see"
    assert _unstamped(calls) == [7], "only the unstamped failure may be reported"
