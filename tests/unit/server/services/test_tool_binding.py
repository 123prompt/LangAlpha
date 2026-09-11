"""The one function that decides which path a granted tool takes."""

from __future__ import annotations

import pytest

from src.server.services.brokerage_capabilities import (
    ALL_BINDINGS,
    _CURATION,
    GROUPS,
    CapabilityGroup,
    group_keys_for,
)
from src.server.services.egress import fold_tool_name
from src.server.services.tool_binding import (
    BindingInputs,
    PRESET_PTC_ONLY,
    Resolved,
    allowed_bindings,
    inputs_from_row,
    merge_overrides,
    resolve_plan,
    resolve_tool,
    strip_disallowed_overrides,
    validate_overrides,
)

MOOMOO = "moomoo"
ALL = ("market_data", "watchlists", "account", "paper_trading", "trading")
LIVE_ORDER_TOOLS = [
    (vendor, tool)
    for vendor, curated in sorted(_CURATION.items())
    for tool in curated.get("trading", ())
]


def test_group_default_binds_paper_trading_direct_and_the_rest_ptc():
    plan = resolve_plan(MOOMOO, ALL, BindingInputs())
    assert "sim_trade_account_list" in plan.direct
    assert "sim_trade_account_list" in plan.sandbox_excluded
    assert plan.by_tool["sim_trade_account_list"].source == "group"
    assert resolve_tool(MOOMOO, "trade_account_list", BindingInputs()).binding == "ptc"


def test_consent_still_gates_what_the_plan_covers():
    plan = resolve_plan(MOOMOO, ("paper_trading",), BindingInputs())
    assert "sim_trade_account_list" in plan.direct
    assert "trading_order_place" not in plan.by_tool


def test_precedence_override_beats_preset_beats_group():
    inputs = BindingInputs(
        overrides={"sim_trade_account_list": "direct"},
        preset=PRESET_PTC_ONLY,
    )
    plan = resolve_plan(MOOMOO, ALL, inputs)
    assert plan.by_tool["sim_trade_account_list"].binding == "direct"
    assert plan.by_tool["sim_trade_account_list"].source == "override"
    assert plan.by_tool["sim_trade_input_order"].source == "preset"
    cleared = resolve_plan(MOOMOO, ALL, BindingInputs(preset=None))
    assert cleared.by_tool["sim_trade_input_order"].source == "group"


@pytest.mark.parametrize("stale", ["order_direct", "no_such_preset"])
def test_a_stored_preset_the_resolver_does_not_know_means_group_defaults(stale):
    """``order_direct`` used to be the switch's off position and may still sit
    in a column. It has to resolve exactly as a cleared row does, or a row the
    user never touched again would carry a binding the page cannot explain."""
    plan = resolve_plan(MOOMOO, ALL, BindingInputs(preset=stale))
    assert plan == resolve_plan(MOOMOO, ALL, BindingInputs())
    assert plan.by_tool["sim_trade_input_order"] == Resolved("direct", "group")
    assert plan.by_tool["trading_order_place"] == Resolved("direct", "group")


def test_ptc_only_preset_sends_everything_it_can_through_the_sandbox():
    plan = resolve_plan(MOOMOO, ALL, BindingInputs(preset=PRESET_PTC_ONLY))
    live = frozenset(_CURATION[MOOMOO]["trading"])
    assert plan.direct == live
    assert plan.sandbox_excluded == live
    assert all(r.binding == "ptc" for t, r in plan.by_tool.items() if t not in live)


def test_both_keeps_a_wrapper_and_a_json_tool():
    inputs = BindingInputs(overrides={"sim_trade_account_list": "both"})
    plan = resolve_plan(MOOMOO, ALL, inputs)
    assert "sim_trade_account_list" in plan.direct
    assert "sim_trade_account_list" not in plan.sandbox_excluded


@pytest.mark.parametrize("preset", [None, PRESET_PTC_ONLY])
@pytest.mark.parametrize("stored", ["direct", "both", "ptc", None])
@pytest.mark.parametrize("vendor,tool", LIVE_ORDER_TOOLS)
def test_a_live_order_tool_is_always_bound_directly(vendor, tool, stored, preset):
    """The clamp nothing on the row can move. A live order is one tool call,
    the shape a per-call stop and a UI can see, so no map and no preset can
    put one back into a sandbox execution where any number can be placed
    inside a single ``execute_code``."""
    overrides = {tool: stored} if stored else {}
    inputs = BindingInputs(overrides=overrides, preset=preset)
    plan = resolve_plan(vendor, group_keys_for(vendor), inputs)
    resolved = plan.by_tool[tool]
    assert resolved.binding == "direct"
    expected_source = "policy" if stored in ("ptc", "both") or (
        stored is None and preset == PRESET_PTC_ONLY
    ) else {"direct": "override"}.get(stored, "group")
    assert resolved.source == expected_source
    assert tool in plan.direct
    assert tool in plan.sandbox_excluded


@pytest.mark.parametrize("spelling", ["TRADING_ORDER_PLACE", " Trading_Order_Place ", "\ttrading_order_place\n"])
@pytest.mark.parametrize("stored", ["ptc", "both"])
def test_a_recased_or_padded_live_order_key_cannot_slip_past_the_clamp(spelling, stored):
    inputs = BindingInputs(overrides={spelling: stored})
    plan = resolve_plan(MOOMOO, ALL, inputs)
    assert plan.by_tool["trading_order_place"].binding == "direct"
    assert plan.by_tool[spelling].binding == "direct"
    assert plan.by_tool[spelling].source == "policy"
    assert validate_overrides(MOOMOO, {spelling: stored})


def test_the_clamp_falls_back_to_the_group_default_not_to_ptc():
    """The floor it replaces could only push a tool one way. The clamp answers
    with whatever the group says its default is, so it holds in either
    direction; a live-order group defaults to ``direct`` and that is what an
    asked-for ``ptc`` becomes."""
    resolved = resolve_tool(MOOMOO, "trading_order_place", BindingInputs(preset=PRESET_PTC_ONLY))
    assert resolved == resolve_tool(MOOMOO, "trading_order_place", BindingInputs(overrides={"trading_order_place": "both"}))
    assert (resolved.binding, resolved.source) == ("direct", "policy")


def test_every_group_default_is_one_of_its_allowed_bindings():
    for group in GROUPS:
        assert group.default_binding in group.allowed_bindings, group.key
    with pytest.raises(ValueError):
        CapabilityGroup(
            key="x", order=0, tone="neutral",
            default_binding="ptc", allowed_bindings=frozenset({"direct"}),
        )


def test_only_live_orders_are_clamped():
    """``order_preview`` and ``staged_orders`` are deliberately untouched: a
    preview persists nothing and a staged order is one human click from live,
    which is the click that makes it a different rung."""
    restricted = {g.key for g in GROUPS if g.allowed_bindings != ALL_BINDINGS}
    assert restricted == {"trading"}
    for vendor, tool in [("robinhood", "review_equity_order"), ("ibkr", "create_order_instruction")]:
        assert allowed_bindings(vendor, tool) == ALL_BINDINGS
        for stored in ("ptc", "direct", "both"):
            assert validate_overrides(vendor, {tool: stored}) is None
            assert resolve_tool(vendor, tool, BindingInputs(overrides={tool: stored})).binding == stored
    assert allowed_bindings(MOOMOO, "no_such_tool") == ALL_BINDINGS


@pytest.mark.parametrize("stored", ["ptc", "both"])
def test_the_write_path_refuses_a_live_order_override_off_the_direct_path(stored):
    """Refused rather than stored and then overruled: a setting the resolver
    contradicts is one the user believes is in force when it never was. The
    reason names what the tool may be, not only what it may not."""
    reason = validate_overrides(MOOMOO, {"trading_order_place": stored})
    assert reason and "direct tool call" in reason
    assert validate_overrides(MOOMOO, {"trading_order_place": "direct"}) is None
    assert validate_overrides(MOOMOO, {"sim_trade_input_order": stored}) is None


def test_row_inputs_read_the_untouched_defaults():
    assert inputs_from_row(None) == BindingInputs()
    row = {"tool_binding": {"x": "direct"}, "binding_preset": ""}
    inputs = inputs_from_row(row)
    assert inputs.overrides == {"x": "direct"}
    assert inputs.preset is None


def test_an_unchanged_stored_entry_is_not_this_requests_doing():
    """A disallowed entry that was stored before the clamp existed must not
    lock the row: resubmitting it unchanged passes, asking for it afresh does
    not."""
    stored = {"trading_order_place": "ptc", "quote_stock_quote": "direct"}
    assert validate_overrides(MOOMOO, stored, stored=stored) is None
    resubmitted = {**stored, "quote_stock_quote": "both"}
    assert validate_overrides(MOOMOO, resubmitted, stored=stored) is None
    changed = {**stored, "trading_order_place": "both"}
    assert "direct tool call" in (validate_overrides(MOOMOO, changed, stored=stored) or "")
    assert "direct tool call" in (validate_overrides(MOOMOO, stored) or "")


def test_the_stored_map_drops_what_the_clamp_overrules():
    """What gets written carries no entry the resolver would answer ``policy``
    to, folded the way ``allowed_bindings`` folds, so a recased key goes too."""
    overrides = {
        "trading_order_place": "ptc",
        " Trading_Order_Cancel ": "both",
        "trading_order_confirm": "direct",
        "sim_trade_input_order": "ptc",
    }
    assert strip_disallowed_overrides(MOOMOO, overrides) == {
        "trading_order_confirm": "direct",
        "sim_trade_input_order": "ptc",
    }
    assert strip_disallowed_overrides(None, overrides) == overrides


def test_a_fold_colliding_map_reports_what_the_split_does():
    """The resolver reads the map the way the split and the relay compare
    names. Two spellings that fold together must report one binding, and it
    must be the binding the sandbox wrapper actually gets or loses."""
    from src.server.services.mcp_tool_split import split_server_tools

    inputs = BindingInputs(
        overrides={"sim_trade_input_order": "both", "SIM_TRADE_INPUT_ORDER": "direct"}
    )
    plan = resolve_plan(MOOMOO, ("paper_trading",), inputs)
    reported = plan.by_tool["sim_trade_input_order"].binding
    assert plan.by_tool["SIM_TRADE_INPUT_ORDER"].binding == reported
    assert reported == "both", "sorted raw keys, later wins"
    sandbox, direct = split_server_tools(
        [{"name": "sim_trade_input_order"}], denied=None, plan=plan
    )
    kept_wrapper = any(t["name"] == "sim_trade_input_order" for t in sandbox)
    assert kept_wrapper == (reported == "both")
    assert direct is not None and direct.schemas[0]["name"] == "sim_trade_input_order"
    assert inputs.overrides == {
        "sim_trade_input_order": "both",
        "SIM_TRADE_INPUT_ORDER": "direct",
    }, "the stored bytes are never rewritten"


def test_a_new_fold_collision_is_refused_but_a_stored_one_does_not_lock_the_row():
    reason = validate_overrides(
        MOOMOO, {"sim_trade_input_order": "both", " Sim_Trade_Input_Order ": "direct"}
    )
    assert reason and "' Sim_Trade_Input_Order '" in reason
    assert "'sim_trade_input_order'" in reason

    stored = {"sim_trade_input_order": "both", "SIM_TRADE_INPUT_ORDER": "direct"}
    assert validate_overrides(MOOMOO, stored, stored=stored) is None
    unrelated = {**stored, "quote_stock_quote": "both"}
    assert validate_overrides(MOOMOO, unrelated, stored=stored) is None
    changed = {**stored, "SIM_TRADE_INPUT_ORDER": "ptc"}
    assert "same tool" in (validate_overrides(MOOMOO, changed, stored=stored) or "")


def test_a_recased_override_for_a_denied_tool_is_not_reported_as_reachable():
    """Consent carries the curated spelling; an override key need not. The plan
    has to drop both, or the tools page offers a binding for a tool the relay
    refuses."""
    inputs = BindingInputs(overrides={"SIM_TRADE_INPUT_ORDER": "direct"})
    plan = resolve_plan(MOOMOO, ("market_data",), inputs)
    assert "SIM_TRADE_INPUT_ORDER" not in plan.by_tool
    assert not any(fold_tool_name(t) == "sim_trade_input_order" for t in plan.direct)


class TestMergeOverrides:
    """The delta the binding endpoint applies to the stored map.

    The page names the tool it changed instead of carrying the map, so the
    reconciliation the client used to do lands here: the resolver reads the
    map folded, and a stored key can be another spelling of the name being
    written.
    """

    def test_set_adds_without_disturbing_the_rest(self):
        stored = {"quote_stock_quote": "direct"}
        assert merge_overrides(stored, set_={"quote_kline": "both"}) == {
            "quote_stock_quote": "direct",
            "quote_kline": "both",
        }

    def test_unset_removes_only_the_tool_it_names(self):
        stored = {"quote_stock_quote": "direct", "quote_kline": "both"}
        assert merge_overrides(stored, unset=["quote_kline"]) == {
            "quote_stock_quote": "direct"
        }

    def test_set_replaces_every_stored_spelling_of_the_tool(self):
        stored = {" EXISTING_TOOL ": "direct", "quote_kline": "both"}
        assert merge_overrides(stored, set_={"existing_tool": "ptc"}) == {
            "quote_kline": "both",
            "existing_tool": "ptc",
        }

    def test_unset_removes_every_stored_spelling_of_the_tool(self):
        stored = {" EXISTING_TOOL ": "direct", "quote_kline": "both"}
        assert merge_overrides(stored, unset=["existing_tool"]) == {
            "quote_kline": "both"
        }

    def test_an_empty_delta_leaves_the_map_alone(self):
        stored = {"quote_stock_quote": "direct"}
        assert merge_overrides(stored) == stored


class TestStdioIsNeverRelayable:
    """A stdio server has no URL, so the relay has nothing to dial.

    The direct path is unreachable for it by construction, and the endpoint
    that offers the choice has to say so rather than accept a binding the
    resolver will not honour.
    """

    def test_a_stdio_row_offers_only_ptc(self):
        assert allowed_bindings(None, "anything", relayable=False) == frozenset({"ptc"})

    def test_an_http_row_is_unrestricted_without_a_group(self):
        assert allowed_bindings(None, "anything") == ALL_BINDINGS

    def test_an_override_asking_for_direct_resolves_to_ptc_as_policy(self):
        inputs = BindingInputs(overrides={"anything": "direct"}, relayable=False)
        resolved = resolve_tool(None, "anything", inputs)
        assert (resolved.binding, resolved.source) == ("ptc", "policy")

    def test_the_same_override_stands_on_a_relayable_row(self):
        inputs = BindingInputs(overrides={"anything": "direct"})
        resolved = resolve_tool(None, "anything", inputs)
        assert (resolved.binding, resolved.source) == ("direct", "override")

    def test_the_write_path_refuses_direct_on_a_stdio_row(self):
        reason = validate_overrides(None, {"anything": "direct"}, relayable=False)
        assert reason and "sandbox wrapper" in reason

    def test_inputs_read_relayable_off_the_row_transport(self):
        assert inputs_from_row({"transport": "stdio"}).relayable is False
        assert inputs_from_row({"transport": "http"}).relayable is True
