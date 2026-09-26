"""Shared result shapes the filesystem backends hand back to the tools."""

from __future__ import annotations

from typing import TypedDict


class EditTextResult(TypedDict, total=False):
    """What ``aedit_text`` reports back.

    ``size`` is the character count of the file after the edit. It is here so a
    caller that has to judge the edited file against a size cap does not read
    the file back through the backend it just wrote through.
    """

    success: bool
    error: str
    message: str
    occurrences: int
    size: int
