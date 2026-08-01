from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from ptc_agent.agent.middleware.background_subagent.workflow.validation import (
    DispatchValidationError,
    validate_dispatch,
)
from src.config.models import WorkflowOrchestrationConfig


def _validate(
    *,
    prompt: str = "research this",
    opts: dict[str, Any] | None = None,
    known: list[str] | None = None,
    default: str = "general",
    caps: WorkflowOrchestrationConfig | None = None,
) -> dict[str, Any]:
    return validate_dispatch(
        prompt=prompt,
        opts=opts or {},
        known_subagent_types=known or ["general", "research"],
        default_subagent_type=default,
        caps=caps or WorkflowOrchestrationConfig(),
    )


def test_defaults_and_unknown_options_are_ignored() -> None:
    assert _validate(opts={"futureOption": True}) == {
        "subagent_type": "general",
        "prompt": "research this",
        "label": None,
        "phase": None,
        "schema": None,
    }


@pytest.mark.parametrize("prompt", ["", None, 7])
def test_prompt_must_be_a_non_empty_string(prompt: Any) -> None:
    with pytest.raises(DispatchValidationError, match="prompt"):
        _validate(prompt=prompt)


def test_prompt_length_cap_is_enforced() -> None:
    caps = WorkflowOrchestrationConfig(max_prompt_chars=3)
    with pytest.raises(DispatchValidationError, match="max_prompt_chars cap is 3"):
        _validate(prompt="four", caps=caps)


@pytest.mark.parametrize("key", ["model", "effort", "isolation"])
def test_unsupported_options_are_rejected(key: str) -> None:
    with pytest.raises(
        DispatchValidationError,
        match=rf"opts\.{key} is not supported in this environment",
    ):
        _validate(opts={key: "value"})


def test_agent_type_is_selected_and_validated() -> None:
    assert _validate(opts={"agentType": "research"})["subagent_type"] == "research"
    with pytest.raises(DispatchValidationError, match="Available: alpha, zeta"):
        _validate(opts={"agentType": "missing"}, known=["zeta", "alpha"])


def test_label_and_phase_are_coerced_and_clipped() -> None:
    result = _validate(opts={"label": 42, "phase": "p" * 130})
    assert result["label"] == "42"
    assert result["phase"] == "p" * 120


@pytest.mark.parametrize("opts", [[], "opts", 7, None])
def test_non_object_opts_is_a_dispatch_error(opts: Any) -> None:
    """``opts`` arrives from JavaScript, so its shape is the script's choice.
    Anything but a mapping has to fail as a dispatch error the script can
    catch — an AttributeError here would escape as a server exception."""
    with pytest.raises(DispatchValidationError, match="plain object"):
        validate_dispatch(
            prompt="research this",
            opts=opts,
            known_subagent_types=["general"],
            default_subagent_type="general",
            caps=WorkflowOrchestrationConfig(),
        )


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string", "pattern": "^(([a-z])+.)+[A-Z]([a-z])+$"},
        {
            "type": "object",
            "properties": {"s": {"type": "string", "pattern": "^(a+)+$"}},
        },
        {"type": "object", "patternProperties": {"^(a+)+$": {"type": "string"}}},
    ],
)
def test_regex_schema_keywords_are_rejected_at_any_depth(
    schema: dict[str, Any],
) -> None:
    """`re` backtracks, so a schema well inside every size cap can burn minutes
    of server CPU validating a short reply. The keywords are refused outright
    rather than bounded."""
    with pytest.raises(DispatchValidationError, match="must not use"):
        _validate(opts={"schema": schema})


def test_schema_must_be_an_object() -> None:
    with pytest.raises(DispatchValidationError, match="opts.schema must be an object"):
        _validate(opts={"schema": []})


def test_schema_serialized_size_cap_is_enforced() -> None:
    caps = WorkflowOrchestrationConfig(schema_max_bytes=256)
    schema = {"type": "string", "description": "x" * 300}
    with pytest.raises(DispatchValidationError, match="schema_max_bytes cap 256"):
        _validate(opts={"schema": schema}, caps=caps)


def test_schema_depth_cap_is_enforced() -> None:
    caps = WorkflowOrchestrationConfig(schema_max_depth=2)
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    with pytest.raises(DispatchValidationError, match="schema_max_depth cap 2"):
        _validate(opts={"schema": schema}, caps=caps)


def test_schema_property_cap_is_enforced_at_any_level() -> None:
    caps = WorkflowOrchestrationConfig(schema_max_properties=2)
    schema = {"type": "object", "properties": {}, "required": []}
    with pytest.raises(DispatchValidationError, match="schema_max_properties cap is 2"):
        _validate(opts={"schema": schema}, caps=caps)


def test_invalid_json_schema_is_rejected() -> None:
    with pytest.raises(DispatchValidationError, match="valid JSON Schema"):
        _validate(opts={"schema": {"type": "not-a-real-type"}})


def test_valid_schema_is_returned_unchanged() -> None:
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    assert _validate(opts={"schema": schema})["schema"] is schema


def test_new_config_field_bounds_are_active() -> None:
    """Only the floors — the defaults themselves are tunable, and pinning them
    reddens the suite on a capacity retune that breaks nothing."""
    with pytest.raises(ValidationError):
        WorkflowOrchestrationConfig(memory_limit_mb=15)
    with pytest.raises(ValidationError):
        WorkflowOrchestrationConfig(max_dispatches_per_run=0)
