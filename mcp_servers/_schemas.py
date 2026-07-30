"""Published output schemas for the market-data MCP servers.

Each tool's return annotation is an ``output_model()`` subclass of
``RootModel[dict]``: any mapping validates (both ``make_response`` and
``make_error`` envelopes — the error contract can never become an ``isError``
exception via schema validation), the returned dict round-trips into
``structuredContent`` unpadded, and the *published* schema is the precise
envelope union injected via ``json_schema_extra``. The published root must be
``type: "object"`` with a sibling ``anyOf`` of required-key sets — never a root
``anyOf`` (breaks pre-2026 ``tools/list`` parsers) and never a Python union
annotation (the SDK wraps unions under ``result``).

Requires pydantic only (ships with the ``mcp`` SDK on every sandbox image);
schema derivation is identical under mcp 1.x and 2.x.
"""

from __future__ import annotations

from typing import Any

from pydantic import RootModel

# ── data-shape descriptors (the `data` property) ──────────────────────────────

#: List of record objects (rows).
RECORDS: dict[str, Any] = {"type": "array", "items": {"type": "object"}}

#: Map keyed by symbol/date/etc. → list of record objects.
RECORDS_BY_KEY: dict[str, Any] = {
    "type": "object",
    "additionalProperties": {"type": "array", "items": {"type": "object"}},
}

#: Single record object.
OBJECT: dict[str, Any] = {"type": "object"}

#: Map keyed by symbol/section/etc. → record object.
OBJECTS_BY_KEY: dict[str, Any] = {
    "type": "object",
    "additionalProperties": {"type": "object"},
}

#: Shape branches on an argument — documented in the tool docstring instead.
ANY: dict[str, Any] = {}

# Standard envelope echo keys (make_response keyword arguments).
_FRAME_PROPS: dict[str, dict[str, Any]] = {
    "symbol": {"type": "string", "description": "Echoed ticker or identifier."},
    "interval": {"type": "string", "description": "Canonical interval echoed back."},
    "currency": {"type": "string", "description": "ISO 4217 currency of price fields."},
    "timezone": {"type": "string", "description": "IANA timezone of timestamps."},
}


def envelope_schema(
    data_shape: dict[str, Any],
    *,
    frame: tuple[str, ...] = (),
    echo: dict[str, dict[str, Any]] | None = None,
    data_description: str | None = None,
) -> dict[str, Any]:
    """Published schema for the success∪error envelope of one tool.

    ``frame`` selects the standard echo keys (symbol/interval/currency/
    timezone); ``echo`` adds tool-specific properties. ``additionalProperties``
    stays true so open-ended echo keys and vendor drift never hard-fail a tool.
    """
    props: dict[str, Any] = {}
    for key in frame:
        props[key] = _FRAME_PROPS[key]
    data = dict(data_shape)
    if data_description:
        data["description"] = data_description
    props["count"] = {"type": "integer", "description": "Number of records in data."}
    props["data"] = data
    props["source"] = {"type": "string", "description": "Upstream data source."}
    if echo:
        props.update(echo)
    props["error"] = {
        "type": "string",
        "description": "Machine-readable error code (error responses only).",
    }
    props["detail"] = {
        "type": "string",
        "description": "Human-readable error detail (error responses only).",
    }
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": props,
        "anyOf": [
            {"required": ["count", "data", "source"]},
            {"required": ["error", "detail"]},
        ],
    }


class _EnvelopeBase(RootModel[dict[str, Any]]):
    """Any mapping validates; subclasses only replace the published schema."""


def output_model(name: str, schema: dict[str, Any]) -> type[_EnvelopeBase]:
    """RootModel[dict] subclass publishing ``schema`` as the tool's outputSchema."""
    return type(name, (_EnvelopeBase,), {"model_config": {"json_schema_extra": schema}})
