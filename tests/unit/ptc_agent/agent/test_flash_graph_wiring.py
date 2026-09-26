"""The Flash builder forwards the per-turn user data the market vote reads."""

from unittest.mock import MagicMock, patch

from ptc_agent.agent.flash.graph import build_flash_graph


def test_build_flash_graph_forwards_user_data_counts():
    counts = {"watchlist_symbols": ["600519.SS"], "portfolio_count": 0}
    with patch("ptc_agent.agent.flash.graph.FlashAgent") as agent_cls:
        agent_cls.return_value.create_agent.return_value = "graph"
        result = build_flash_graph(
            config=MagicMock(), user_profile={"name": "a"}, user_data_counts=counts
        )

    assert result == "graph"
    kwargs = agent_cls.return_value.create_agent.call_args.kwargs
    assert kwargs["user_data_counts"] is counts
