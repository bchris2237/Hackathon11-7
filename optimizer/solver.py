"""
TradeMind optimizer — public API (stable import surface for the backend).
Owner: Developer 1.

All implementation lives in optimizer.engine and optimizer.feasibility.
Do not import engine or feasibility directly from outside the optimizer package.

Public surface (A1 + A2 + A3):
  solve(scenario)                  → Result
  optimize(scenario)               → Result   [A2 alias, preferred by backend]
  enumerate_assignments(scenario)  → list[dict[str,str]]   [A2]
  verify(scenario, result)         → Result
  validate_assignment(scenario, assignments) → list[AssignmentViolation]   [A3]
  explain_infeasibility(scenario)            → InfeasibilityReport          [A3]
"""
from contracts.models import Result, Scenario

from optimizer.engine import enumerate_assignments as _enumerate_assignments
from optimizer.engine import optimize as _optimize
from optimizer.engine import solve as _solve
from optimizer.engine import verify as _verify
from optimizer.feasibility import (
    AssignmentViolation,
    InfeasibilityReport,
    explain_infeasibility as _explain_infeasibility,
    validate_assignment as _validate_assignment,
)


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


def validate_assignment(
    scenario: Scenario,
    assignments: list,
) -> list[AssignmentViolation]:
    """
    Validate a fully specified assignment list against a confirmed scenario.

    Returns a (possibly empty) list of AssignmentViolation objects.
    Empty list means the assignment satisfies all constraints.
    Does NOT raise — the caller decides how to handle violations.

    Checks: coverage, known IDs, service compatibility, correct duration,
            capacity constraints, and no-worse-off (netSavings >= 0).
    """
    return _validate_assignment(scenario, assignments)


def explain_infeasibility(scenario: Scenario) -> InfeasibilityReport:
    """
    Analyse a scenario and return a structured InfeasibilityReport.

    Reports observed constraint violations based on self-assignment analysis:
      - capacity_violations: businesses where self-assignment exceeds capacity
      - savings_violations:  businesses where self-assignment yields negative net savings
      - unsupported_services: deliverables whose owner has no estimate (MISSING_BASELINE)

    Does NOT claim to compute a minimal unsatisfiable core.
    Used by the backend /explain route and INFEASIBLE result diagnostics.
    """
    return _explain_infeasibility(scenario)


# Re-export types so backend can use them without importing feasibility directly
__all__ = [
    "solve",
    "optimize",
    "enumerate_assignments",
    "verify",
    "validate_assignment",
    "explain_infeasibility",
    "AssignmentViolation",
    "InfeasibilityReport",
]
