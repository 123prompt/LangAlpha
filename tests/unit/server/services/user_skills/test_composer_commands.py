"""The composer's slash triggers are declared in the frontend and mirrored in
Python. Nothing at runtime reads both, so only this test stops them drifting
when a seventh composer command lands.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.server.services.user_skills.validate import (
    COMPOSER_COMMANDS,
    reserved_skill_names,
)

HELPERS = (
    Path(__file__).resolve().parents[5]
    / "web/src/components/ui/chat-input.helpers.tsx"
)


def _declared_triggers() -> set[str]:
    """Every name and alias inside the BUILTIN_SLASH_COMMANDS array literal."""
    src = HELPERS.read_text()
    m = re.search(r"BUILTIN_SLASH_COMMANDS\s*=\s*\[(.*?)\n\];", src, re.DOTALL)
    assert m, "BUILTIN_SLASH_COMMANDS array literal not found"
    body = m.group(1)
    triggers = set(re.findall(r"name:\s*'([^']+)'", body))
    for group in re.findall(r"aliases:\s*\[([^\]]*)\]", body):
        triggers.update(re.findall(r"'([^']+)'", group))
    return triggers


@pytest.mark.skipif(not HELPERS.is_file(), reason="frontend tree not present")
def test_python_mirror_matches_the_frontend_declaration():
    assert _declared_triggers() == set(COMPOSER_COMMANDS)


def test_composer_triggers_are_reserved_against_user_skills():
    assert COMPOSER_COMMANDS <= reserved_skill_names()
