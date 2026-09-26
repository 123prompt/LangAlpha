"""Per-turn read path: the session-cached MCP tool summary is plumbed into
create_agent and reused byte-stable across turns (no per-turn recompute).

The summary is what the runtime-context baseline freezes as its
``<mcp-servers>`` block. Regression #6 at the session-cache layer: two
consecutive turns of the same session pass the IDENTICAL cached string into
create_agent, so the hot path never re-resolves or recomputes. A recomputed
string that merely reordered would now read as a roster change and file a row
saying nothing changed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ptc_agent.agent.graph import build_ptc_graph_with_session


def _make_session(summary):
    session = MagicMock()
    session.conversation_id = "ws-1"
    session.sandbox = MagicMock()
    session.sandbox.vault_secrets = None
    session.mcp_registry = MagicMock()
    session.mcp_tool_summary = summary
    return session


@pytest.mark.asyncio
async def test_session_summary_passed_to_create_agent():
    session = _make_session("CACHED-SUMMARY")
    config = MagicMock()
    config.subagents = MagicMock(enabled=[])

    fake_agent = MagicMock()
    fake_agent.create_agent = MagicMock(return_value="GRAPH")

    with patch(
        "ptc_agent.agent.graph.PTCAgent", return_value=fake_agent
    ), patch(
        "ptc_agent.agent.graph.fetch_user_data_counts",
        new=AsyncMock(return_value=None),
    ):
        await build_ptc_graph_with_session(session=session, config=config)

    kwargs = fake_agent.create_agent.call_args.kwargs
    assert kwargs["tool_summary"] == "CACHED-SUMMARY"


@pytest.mark.asyncio
async def test_two_turns_pass_identical_cached_summary():
    """The cached summary string is identical across consecutive turns: the
    per-turn path reads it, never recomputes it, so the text the baseline froze
    is the text every later turn is compared against."""
    session = _make_session("STABLE-SUMMARY")
    config = MagicMock()
    config.subagents = MagicMock(enabled=[])

    summaries = []
    fake_agent = MagicMock()

    def capture(**kwargs):
        summaries.append(kwargs["tool_summary"])
        return "GRAPH"

    fake_agent.create_agent = MagicMock(side_effect=capture)

    with patch(
        "ptc_agent.agent.graph.PTCAgent", return_value=fake_agent
    ), patch(
        "ptc_agent.agent.graph.fetch_user_data_counts",
        new=AsyncMock(return_value=None),
    ):
        await build_ptc_graph_with_session(session=session, config=config)
        await build_ptc_graph_with_session(session=session, config=config)

    assert summaries == ["STABLE-SUMMARY", "STABLE-SUMMARY"]
    assert summaries[0] is summaries[1]


@pytest.mark.asyncio
async def test_shared_session_graph_redacts_its_own_workspace_secrets():
    from ptc_agent.core.project_context import ProjectContext
    from ptc_agent.agent.middleware.tool.leak_detection import LeakDetectionMiddleware

    session = _make_session('summary')
    session.sandbox.vault_secrets = {'KEY': 'synthetic-sibling-value'}
    agent = MagicMock()
    with (
        patch('ptc_agent.agent.graph.PTCAgent', return_value=agent),
        patch('ptc_agent.agent.graph._read_workspace_naming', AsyncMock(return_value=('A', ''))),
        patch('src.server.database.vault_secrets.get_effective_secrets',
              AsyncMock(return_value={'KEY': 'synthetic-project-value'})) as secrets,
    ):
        await build_ptc_graph_with_session(
            session=session, config=MagicMock(), project=ProjectContext('ws-a', 'a'),
        )
    secrets.assert_awaited_once_with('ws-a', user_id=None)
    middleware = LeakDetectionMiddleware(vault_secrets=agent.create_agent.call_args.kwargs['vault_secrets'])
    session.sandbox.vault_secrets = {'KEY': 'synthetic-third-value'}
    assert 'synthetic-project-value' not in middleware.redact('result synthetic-project-value')
    assert middleware.redact('synthetic-sibling-value') == 'synthetic-sibling-value'


@pytest.mark.asyncio
async def test_graph_does_not_continue_with_wrong_secrets_when_vault_read_fails():
    from ptc_agent.core.project_context import ProjectContext

    agent = MagicMock()
    with (
        patch('ptc_agent.agent.graph.PTCAgent', return_value=agent),
        patch('ptc_agent.agent.graph._read_workspace_naming', AsyncMock(return_value=('A', ''))),
        patch('src.server.database.vault_secrets.get_effective_secrets',
              AsyncMock(side_effect=RuntimeError('vault unavailable'))),
        pytest.raises(RuntimeError, match='vault unavailable'),
    ):
        await build_ptc_graph_with_session(
            session=_make_session('summary'), config=MagicMock(), project=ProjectContext('ws-a', 'a'),
        )
    agent.create_agent.assert_not_called()
