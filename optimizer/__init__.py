"""Deterministic optimizer. Owner: Developer 1 (after the Phase 0 handoff).

STATUS: NOT IMPLEMENTED. Prompts A1-A6 build this package. Until then
`optimize` raises instead of returning plausible-looking numbers, so nothing
can mistake this placeholder for a working solver.
"""

from __future__ import annotations

from typing import Any

STATUS = "not_implemented"


class OptimizerNotImplementedError(NotImplementedError):
    """Raised by the Phase 0 placeholder."""


def optimize(scenario: Any) -> Any:
    raise OptimizerNotImplementedError(
        "The optimizer is not implemented yet (Phase 0 placeholder, prompts A1-A6)."
    )
