from datetime import UTC, datetime

from ptc_agent.agent.context_stack import build_context_middleware
from ptc_agent.agent.middleware.runtime_context import TurnContext


def _stack(timezone, **kwargs):
    return build_context_middleware(
        now=datetime(2026, 9, 13, 6, 0, tzinfo=UTC),
        guidance=None,
        model_name=None,
        sandbox_enabled=False,
        turn_context=TurnContext(timezone=timezone),
        **kwargs,
    )


def test_a_failed_profile_read_leaves_the_zone_to_the_frozen_identity():
    """A build with no profile passes None, so the turn row reads the zone
    the identity block states instead of stamping UTC over it."""
    assert _stack(timezone=None, user_profile=None).turn._timezone is None
    assert (
        _stack(timezone="Asia/Shanghai", user_profile={"timezone": "Asia/Shanghai"})
        .turn._timezone
        == "Asia/Shanghai"
    )


def test_a_profile_with_no_zone_states_the_one_the_turn_resolved():
    """The turn's tools read a local time in the zone it resolved (request,
    then locale) when the profile names none, so the identity block states
    that zone rather than UTC."""
    stack = _stack(timezone="America/Chicago", user_profile={"name": "A"})
    assert stack.turn._timezone == "America/Chicago"
    assert stack.baseline._identity().timezone == "America/Chicago"


def test_a_profile_zone_the_turn_skipped_is_not_stated():
    """A profile zone that names no zone is skipped for the request's, and the
    identity block states the one the stamp uses, not the stale one."""
    stack = _stack(timezone="America/Chicago", user_profile={"timezone": "Not/AZone"})
    assert stack.baseline._identity().timezone == "America/Chicago"
