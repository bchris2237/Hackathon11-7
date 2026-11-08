"""Deterministic optimizer (Developer 1's package).

STATUS: IMPLEMENTED. The exact bounded solver, independent certificate, what-if engine and analysis helpers live in the modules of
this package; `optimizer.api` is the dict-in, dict-out seam the backend imports:

    from optimizer.api import optimize, simulate, verify

Nothing here calls AWS, a language model, a network or a file. See docs/integration-notes.md for how the package relates to the
shared contract (`optimizer.contract` holds the plain-typed models this package uses; the wire format is the frozen v1 schema).
"""

from __future__ import annotations

from optimizer.api import optimize, simulate, verify

STATUS = "implemented"

__all__ = ["STATUS", "optimize", "simulate", "verify"]
