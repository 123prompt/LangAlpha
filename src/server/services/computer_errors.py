"""Refusals a computer action raises that the client can act on.

Each subclasses RuntimeError so every existing caller keeps treating them as a
refused action (the workspace aliases still answer 400 with the message); the
computer routes map them to a 409 with a code the UI switches on. Kept out of
the ComputerManager package so the router can import them without loading the
provider stack.
"""

from __future__ import annotations


class MachineBusyError(RuntimeError):
    """An agent turn is running on the machine, so it cannot be replaced now."""


class ComputerBusyError(RuntimeError):
    """Another operation holds the machine or moved it underneath this one; retry later."""


class DiskTooSmallError(RuntimeError):
    """The machine's backed-up files would not fit the smaller disk a downgrade asks for."""


class SpecChangeLostError(RuntimeError):
    """Another request took the change over; this runner stops before it touches the machine."""
