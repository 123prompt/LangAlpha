"""Which path a granted MCP tool takes to the model: sandbox wrapper, JSON tool, or both.

One function decides, and everything that needs the answer calls it: the
resolver when it shapes a workspace's toolset, the grant sync when it records
what the relay must refuse to a sandbox caller, the tools endpoint when it
shows the user what is in force. Three readers computing the set three ways is
how the one-path-per-tool invariant would quietly stop holding.

Precedence, highest first: the user's per-tool override, the row's
``ptc_only`` preset, the group's default, and finally ``ptc``. The answer is then
clamped to the group's ``allowed_bindings``: one outside that set is replaced
by the group's default and reported as ``policy``, the source meaning nothing
on the row could move it. A group that names a single path therefore holds
every tool on it, whatever a map or a preset says, and the same clamp works
in either direction rather than only keeping a tool off the JSON path.

The preset has one value. A null preset is not a second setting but the
absence of one: each group's own default applies, which is what the row does
untouched, so turning the row switch off clears the column rather than
storing a word that would resolve to the same answer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from src.server.services.brokerage_capabilities import (
    ALL_BINDINGS,
    BINDINGS,
    Binding,
    CapabilityGroup,
    denied_tools,
    group_for_tool,
    tools_for,
)
from src.server.services.egress import fold_tool_name, folded_contains

__all__ = [
    "ALL_BINDINGS",
    "BINDINGS",
    "Binding",
    "BindingInputs",
    "BindingPlan",
    "BindingSource",
    "PRESETS",
    "PRESET_PTC_ONLY",
    "Resolved",
    "allowed_bindings",
    "inputs_from_row",
    "merge_overrides",
    "resolve_plan",
    "resolve_tool",
    "strip_disallowed_overrides",
    "validate_overrides",
]

# The one row-level preset a user can pick. ``ptc_only`` sends every tool
# through the sandbox regardless of what a map or a group default says. It does
# not reach past the clamp: a group that allows one path keeps it.
PRESET_PTC_ONLY = "ptc_only"
PRESETS: frozenset[str] = frozenset({PRESET_PTC_ONLY})

BindingSource = Literal["policy", "override", "preset", "group", "default"]


@dataclass(frozen=True)
class Resolved:
    binding: Binding
    source: BindingSource


@dataclass(frozen=True)
class BindingPlan:
    """The per-server answer, split the way its readers consume it."""

    #: Bound to the model as JSON tools (``direct`` and ``both``).
    direct: frozenset[str] = frozenset()
    #: Kept out of the sandbox wrappers, and refused to a sandbox caller at the
    #: relay (``direct`` only).
    sandbox_excluded: frozenset[str] = frozenset()
    by_tool: Mapping[str, Resolved] = field(default_factory=dict)


@dataclass(frozen=True)
class BindingInputs:
    """Everything a row contributes. Defaults are what an untouched row says.

    ``overrides`` is the map exactly as stored and is what gets written back.
    ``folded_overrides`` is the view the resolver reads, keyed the way every
    consumer of a plan compares names, so the binding reported for a tool is
    the binding the split and the relay act on whatever spelling the map used.
    """

    overrides: Mapping[str, str] = field(default_factory=dict)
    preset: str | None = None
    #: Whether the relay has an address to dial for this row at all. A stdio
    #: server has no URL by construction, so the direct path cannot exist for
    #: it however the row or its group is configured.
    relayable: bool = True
    folded_overrides: Mapping[str, str] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        # Two raw keys can fold together in a map stored before the write path
        # refused that. The tie-break is arbitrary but deterministic: raw keys
        # in sorted order, later wins. ``validate_overrides`` keeps new maps
        # from creating another such pair, so this decides legacy rows only.
        view = {fold_tool_name(k): self.overrides[k] for k in sorted(self.overrides)}
        object.__setattr__(self, "folded_overrides", view)


def inputs_from_row(row: Mapping[str, object] | None) -> BindingInputs:
    """A catalog row's contribution; an absent row is an untouched one."""
    if not row:
        return BindingInputs()
    overrides = row.get("tool_binding") or {}
    return BindingInputs(
        overrides=dict(overrides) if isinstance(overrides, Mapping) else {},
        preset=row.get("binding_preset") or None,  # type: ignore[arg-type]
        relayable=row.get("transport") != "stdio",
    )


def allowed_bindings(
    vendor: str | None, tool: str, *, relayable: bool = True
) -> frozenset[Binding]:
    """The paths this tool may take, from its group; every path when it has none.

    The one question the resolver, the write path and the tools endpoint all
    ask, so a stored map, a rejected PATCH and the options the page offers
    cannot disagree about what a tool is allowed to be.

    An unrelayable row answers ``ptc`` before the group is consulted: there is
    no address to dial, so no group policy can put a tool on the direct path.
    """
    if not relayable:
        return frozenset({"ptc"})
    group = group_for_tool(vendor, tool)
    return group.allowed_bindings if group is not None else ALL_BINDINGS


def _ladder(group: CapabilityGroup | None, tool: str, inputs: BindingInputs) -> Resolved:
    override = inputs.folded_overrides.get(fold_tool_name(tool))
    if override in BINDINGS:
        return Resolved(override, "override")  # type: ignore[arg-type]
    if inputs.preset == PRESET_PTC_ONLY:
        return Resolved("ptc", "preset")
    if group is not None and group.default_binding != "ptc":
        return Resolved(group.default_binding, "group")
    return Resolved("ptc", "default")


def resolve_tool(vendor: str | None, tool: str, inputs: BindingInputs) -> Resolved:
    """One tool's binding and where it came from."""
    group = group_for_tool(vendor, tool)
    resolved = _ladder(group, tool, inputs)
    if not inputs.relayable:
        return resolved if resolved.binding == "ptc" else Resolved("ptc", "policy")
    if group is not None and resolved.binding not in group.allowed_bindings:
        return Resolved(group.default_binding, "policy")
    return resolved


def resolve_plan(
    vendor: str | None,
    granted: Iterable[str],
    inputs: BindingInputs,
    *,
    candidates: Iterable[str] = (),
) -> BindingPlan:
    """The plan over every tool consent lets through.

    The candidate set is what the vendor's curation grants plus whatever a map
    or an override names, so a server we curate nothing for still gets the
    bindings its own config asks for. ``candidates`` adds the discovered names
    when the caller has them, which is how an unclassified tool the vendor
    added later shows up with its ``default`` binding on the tools page.
    """
    granted = tuple(granted)
    names: set[str] = set(candidates)
    names.update(tools_for(vendor, granted) or ())
    names.update(inputs.overrides)
    refused = denied_tools(vendor, granted) or frozenset()
    # Folded, because the denial carries the curated spelling while a name here
    # may be an override key or a discovered one. A raw difference would leave a
    # recased spelling of a denied tool in ``by_tool`` reporting a binding the
    # user cannot reach, since the split applies the same denial folded.
    names = {n for n in names if not folded_contains(refused, n)}

    by_tool = {tool: resolve_tool(vendor, tool, inputs) for tool in sorted(names)}
    direct = frozenset(t for t, r in by_tool.items() if r.binding in ("direct", "both"))
    excluded = frozenset(t for t, r in by_tool.items() if r.binding == "direct")
    return BindingPlan(direct, excluded, by_tool)


_PATH_WORDS: dict[str, str] = {
    "ptc": "a sandbox wrapper",
    "direct": "a direct tool call",
    "both": "both at once",
}


def _disallowed(
    vendor: str | None, tool: str, binding: str, relayable: bool = True
) -> bool:
    return binding not in allowed_bindings(vendor, tool, relayable=relayable)


def merge_overrides(
    stored: Mapping[str, str],
    *,
    set_: Mapping[str, str] | None = None,
    unset: Iterable[str] | None = None,
) -> dict[str, str]:
    """``stored`` with a per-tool delta applied.

    A stored key can be another spelling of the name being written. The
    resolver reads the map folded, so the delta replaces every key that folds
    to the one it names instead of leaving a second spelling beside it.
    """
    touched = {fold_tool_name(n) for n in (set_ or {})}
    touched |= {fold_tool_name(n) for n in (unset or ())}
    merged = {k: v for k, v in stored.items() if fold_tool_name(k) not in touched}
    merged.update(set_ or {})
    return merged


def validate_overrides(
    vendor: str | None,
    overrides: Mapping[str, str],
    *,
    stored: Mapping[str, str] | None = None,
    relayable: bool = True,
) -> str | None:
    """The reason a set of overrides cannot be stored, or None.

    A map asking for a path the tool's group does not allow is refused rather
    than quietly coerced: :func:`resolve_tool` would clamp it anyway, and a
    stored value the resolver contradicts is how a user comes to believe a
    setting is in force that never was.

    Two keys that fold to one name are refused the same way. The resolver
    reads the map folded, so a user cannot mean both entries at once, and a
    map that never holds such a pair leaves the resolver's tie-break with only
    rows written before this check to decide.

    Only what the caller is asking for is judged. An entry already in ``stored``
    and resubmitted unchanged is not this request's doing, and refusing it
    would lock the row: every later edit carries the old map forward, so an
    entry that got in before the clamp existed would 422 the write meant to
    change something else. :func:`strip_disallowed_overrides` cleans those up
    instead.
    """
    stored = stored or {}
    for tool, binding in overrides.items():
        if binding not in BINDINGS:
            return f"{tool!r}: binding must be one of {sorted(BINDINGS)}"
        if stored.get(tool) == binding:
            continue
        if _disallowed(vendor, tool, binding, relayable):
            allowed = sorted(allowed_bindings(vendor, tool, relayable=relayable))
            return (
                f"{tool!r} can only be bound as "
                + " or ".join(_PATH_WORDS[b] for b in allowed)
            )
    spellings: dict[str, list[str]] = {}
    for tool in overrides:
        spellings.setdefault(fold_tool_name(tool), []).append(tool)
    for names in spellings.values():
        if len(names) < 2:
            continue
        if all(stored.get(tool) == overrides[tool] for tool in names):
            continue
        first, second = sorted(names)[:2]
        return (
            f"{first!r} and {second!r} name the same tool, so only one of "
            "them can carry a binding"
        )
    return None


def strip_disallowed_overrides(
    vendor: str | None, overrides: Mapping[str, str], relayable: bool = True
) -> dict[str, str]:
    """The map with every entry the clamp would overrule removed.

    What actually gets stored, so a row self-heals on its next write rather
    than carrying an entry the resolver reports as ``policy`` and the page
    therefore cannot offer to reset.
    """
    return {
        tool: binding
        for tool, binding in overrides.items()
        if not _disallowed(vendor, tool, binding, relayable)
    }
