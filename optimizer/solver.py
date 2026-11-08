"""
TradeMind optimizer — public API (stable import surface for the backend).
Owner: Developer 1.

All implementation lives in optimizer.engine.
Do not import engine directly from outside the optimizer package.

Public surface (A1 + A2):
  solve(scenario)                  → Result
  optimize(scenario)               → Result   [A2 alias, preferred by backend]
  enumerate_assignments(scenario)  → list[dict[str,str]]   [A2]
  verify(scenario, result)         → Result
"""
from contracts.models import Result, Scenario

from optimizer.engine import enumerate_assignments as _enumerate_assignments
from optimizer.engine import optimize as _optimize
from optimizer.engine import solve as _solve
from optimizer.engine import verify as _verify


def solve(scenario: Scenario) -> Result:
    """
    Find the unique optimal assignment for the scenario.

    Returns Result with status collaboration | no_exchange | infeasible.
    Raises optimizer.errors.MissingBaselineError (→ 422) when an owner
    has no estimate for one of its own deliverable services.
    """
    return _solve(scenario)


def optimize(scenario: Scenario) -> Result:
    """
    Alias for solve() — A2 public entrypoint name used by the backend.
    Identical behaviour; either name is stable for B4 integration.
    """
    return _optimize(scenario)


def enumerate_assignments(scenario: Scenario) -> list[dict[str, str]]:
    """
    Return every candidate assignment map {deliverableId: providerId} for the
    scenario, in deterministic order (deliverables sorted lexicographically,
    providers owner-first then lexicographic).

    No feasibility filtering — raw search space only.
    Upper bound: 5^6 = 15 625 candidates.
    Raises ValueError if scenario exceeds supported bounds.
    """
    return _enumerate_assignments(scenario)


def verify(scenario: Scenario, result: Result) -> Result:
    """
    Independently recompute the result from scenario + assignment list.
    Returns a fresh Result with a new certificate.
    Raises ValueError if the supplied assignments are inconsistent.

    Call this before passing computed values to Bedrock (/explain).
    Never trust client-supplied arithmetic.
    """
    return _verify(scenario, result)
