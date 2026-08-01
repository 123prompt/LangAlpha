"""Reading a schema'd child's reply back.

The child is instructed to answer with bare JSON, but it is a model, so the
reply routinely arrives fenced or wrapped in a sentence. Anything the child
actually got right has to survive that packaging — a discarded reply costs a
corrective re-dispatch and then a null in the script.
"""

from __future__ import annotations

import pytest

from ptc_agent.agent.middleware.background_subagent.workflow.validation import (
    parse_schema_result,
)

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}


@pytest.mark.parametrize(
    ("content", "case"),
    [
        ('{"ok": true}', "bare"),
        ('```json\n{"ok": true}\n```', "fenced with a language tag"),
        ("```\n{\"ok\": true}\n```", "fenced bare"),
        ('  \n {"ok": true}\n  ', "surrounding whitespace"),
        ('Here are the results:\n{"ok": true}', "prose before"),
        ('{"ok": true}\nThat is the summary.', "prose after"),
        ('Results [see below]:\n{"ok": true}', "a bracket in the prose before"),
        ('{"ok": true}\nSee the [docs] for more.', "a bracket in the prose after"),
        ('I found [1, 2] items:\n{"ok": true}', "valid JSON in the prose before"),
    ],
)
def test_a_schema_satisfying_reply_survives_its_packaging(
    content: str, case: str
) -> None:
    valid, parsed, error = parse_schema_result(content, SCHEMA)
    assert valid, f"{case}: {error}"
    assert parsed == {"ok": True}


def test_a_top_level_array_is_read() -> None:
    valid, parsed, _ = parse_schema_result(
        'The list:\n[1, 2, 3]', {"type": "array"}
    )
    assert valid
    assert parsed == [1, 2, 3]


@pytest.mark.parametrize(
    ("content", "case"),
    [
        ("no JSON here at all", "nothing to parse"),
        ("", "empty reply"),
        ('{"ok": "yes"}', "parses but violates the schema"),
        ('{"missing": true}', "parses but omits a required key"),
        ('{"ok": tru', "truncated mid-value"),
    ],
)
def test_a_reply_that_does_not_satisfy_the_schema_is_rejected(
    content: str, case: str
) -> None:
    valid, parsed, error = parse_schema_result(content, SCHEMA)
    assert not valid, case
    assert parsed is None
    assert error


@pytest.mark.parametrize(
    ("content", "schema", "expected"),
    [
        ("42", {"type": "number"}, 42),
        ("  42  ", {"type": "number"}, 42),
        ("```json\n42\n```", {"type": "number"}, 42),
        ('"done"', {"type": "string"}, "done"),
        ("true", {"type": "boolean"}, True),
        ("null", {"type": "null"}, None),
    ],
)
def test_a_scalar_schema_accepts_a_scalar_reply(
    content: str, schema: dict[str, object], expected: object
) -> None:
    """A scalar is a legal dispatch schema, and a scalar reply carries no
    delimiter to scan from. Scanning alone rejected every one of these, so the
    script got ``null`` no matter what the child answered."""
    valid, parsed, error = parse_schema_result(content, schema)
    assert valid, error
    assert parsed == expected


def test_a_scalar_schema_still_rejects_a_non_conforming_reply() -> None:
    valid, parsed, error = parse_schema_result("about seven", {"type": "number"})
    assert not valid
    assert parsed is None
    assert error


def test_the_candidate_that_satisfies_the_schema_wins() -> None:
    """A bracketed aside can itself be valid JSON. Whichever candidate matches
    the schema is the reply; an earlier one that merely parses is not."""
    valid, parsed, _ = parse_schema_result(
        'Checked [{"ok": "not-a-boolean"}] first.\n{"ok": true}', SCHEMA
    )
    assert valid
    assert parsed == {"ok": True}
