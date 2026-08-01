from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
    WorkflowHostError,
    WorkflowLimits,
    run_workflow_script,
)

from .conftest import workflow_script as _script


class PreludeHost:
    async def agent(self, prompt: str, opts: dict[str, Any]) -> Any:
        if prompt == "fail":
            raise WorkflowHostError("dispatch failed")
        return prompt

    def phase(self, title: str) -> None:
        pass

    def log(self, message: str) -> None:
        pass


@pytest.mark.asyncio
async def test_host_bindings_are_unreachable_after_the_prelude() -> None:
    """The raw host bindings take their arguments unchecked and skip the
    wrappers' argument validation, so the prelude captures them into closure
    scope and drops the globals. Only the wrappers stay reachable."""
    outcome = await run_workflow_script(
        _script(
            "return ["
            "  typeof globalThis.__host_agent,"
            "  typeof globalThis.__host_phase,"
            "  typeof globalThis.__host_log,"
            "  typeof agent"
            "];"
        ),
        None,
        PreludeHost(),
        WorkflowLimits(),
    )
    assert outcome.status == "completed"
    assert outcome.result == ["undefined", "undefined", "undefined", "function"]


@pytest.mark.parametrize(
    "name", ["agent", "phase", "log", "parallel", "pipeline"]
)
@pytest.mark.asyncio
async def test_prelude_helpers_cannot_be_rebound(name: str) -> None:
    """Integrity, not a security boundary: dispatch validation runs host-side in
    Python, so a script that rebinds ``agent`` reaches nothing and fools only
    itself. The freeze is what turns a stray assignment into a loud failure
    instead of a run that silently dispatches no work."""
    outcome = await run_workflow_script(
        _script(f"globalThis.{name} = () => 'shim'; return 1;"),
        None,
        PreludeHost(),
        WorkflowLimits(),
    )

    assert outcome.status == "script_error"
    assert "read-only" in (outcome.error or "")


@pytest.mark.asyncio
async def test_a_rebind_attempt_leaves_the_real_helper_in_place() -> None:
    outcome = await run_workflow_script(
        _script(
            "try { globalThis.agent = async () => 'shim'; } catch (e) {} "
            "return await agent('reached-the-host');"
        ),
        None,
        PreludeHost(),
        WorkflowLimits(),
    )

    # PreludeHost echoes the prompt, so the real host answered — not the shim.
    assert outcome.result == "reached-the-host"


@pytest.mark.asyncio
async def test_parallel_preserves_success_order() -> None:
    outcome = await run_workflow_script(
        _script("return await parallel([async () => 1, async () => 2, async () => 3]);"),
        None,
        PreludeHost(),
        WorkflowLimits(),
    )
    assert outcome.result == [1, 2, 3]


@pytest.mark.asyncio
async def test_parallel_turns_thunk_failures_into_null_slots() -> None:
    outcome = await run_workflow_script(
        _script(
            "return await parallel(["
            "async () => 'left', "
            "async () => { throw new Error('nope'); }, "
            "async () => 'right'"
            "]);"
        ),
        None,
        PreludeHost(),
        WorkflowLimits(),
    )
    assert outcome.result == ["left", None, "right"]


@pytest.mark.asyncio
async def test_parallel_absorbs_dispatch_time_agent_throw() -> None:
    outcome = await run_workflow_script(
        _script(
            "return await parallel(["
            "async () => agent('ok'), async () => agent('fail'), "
            "async () => agent('also-ok')"
            "]);"
        ),
        None,
        PreludeHost(),
        WorkflowLimits(),
    )
    assert outcome.result == ["ok", None, "also-ok"]


@pytest.mark.asyncio
async def test_pipeline_two_stages_receive_prev_original_and_index() -> None:
    outcome = await run_workflow_script(
        _script(
            "return await pipeline([2, 4], "
            "async (prev, original, index) => prev + original + index, "
            "async (prev, original, index) => ({ prev, original, index })"
            ");"
        ),
        None,
        PreludeHost(),
        WorkflowLimits(),
    )
    assert outcome.result == [
        {"prev": 4, "original": 2, "index": 0},
        {"prev": 9, "original": 4, "index": 1},
    ]


@pytest.mark.asyncio
async def test_pipeline_failure_is_per_item_and_skips_later_stages() -> None:
    outcome = await run_workflow_script(
        _script(
            "const later = []; "
            "const result = await pipeline([1, 2, 3], "
            "async (prev) => { if (prev === 2) throw new Error('bad'); return prev * 10; }, "
            "async (prev, original) => { later.push(original); return prev + 1; }"
            "); return { result, later };"
        ),
        None,
        PreludeHost(),
        WorkflowLimits(),
    )
    assert outcome.result == {"result": [11, None, 31], "later": [1, 3]}


@pytest.mark.asyncio
async def test_pipeline_has_no_cross_item_stage_barrier() -> None:
    calls: list[str] = []

    class OrderingHost(PreludeHost):
        async def agent(self, prompt: str, opts: dict[str, Any]) -> Any:
            calls.append(prompt)
            if prompt == "s1-A":
                await asyncio.sleep(0.2)
            elif prompt == "s1-B":
                await asyncio.sleep(0.01)
            return prompt

    outcome = await run_workflow_script(
        _script(
            "return await pipeline(['A', 'B'], "
            "async (_prev, item) => agent('s1-' + item), "
            "async (_prev, item) => agent('s2-' + item)"
            ");"
        ),
        None,
        OrderingHost(),
        WorkflowLimits(),
    )
    assert outcome.status == "completed"
    assert calls.index("s2-B") < calls.index("s2-A")


