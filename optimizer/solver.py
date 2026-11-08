"""
TradeMind optimizer — public API (stable import surface for the backend).
Owner: Developer 1.

All implementation lives in optimizer.engine, optimizer.feasibility, optimizer.metrics.
Do not import those modules directly from outside the optimizer package.

Public surface (A1 + A2 + A3 + A4):
  solve(scenario)                  → Result
  optimize(scenario)               → Result   [A2 alias, preferred by backend]
  enumerate_assignments(scenario)  → list[dict[str,str]]
  verify(scenario, result)         → Result
  validate_assignment(scenario, assignments) → list[AssignmentViolation]
  explain_infeasibility(scenario)            → InfeasibilityReport
  compute_metrics(scenario, assignment_map)  → (list[BusinessMetrics], Totals)  [A4]
  rank_allocation(candidates)                → list[ScoredCandidate]              [A4]
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
from optimizer.metrics import (
    ScoredCandidate,
    compute_metrics as _compute_metrics,
    rank_allocation as _rank_allocation,
)
from optimizer.certificate import (
    VerificationResult,
    generate_certificate as _generate_certificate,
    verify_certificate as _verify_certificate,
)


# ---------------------------------------------------------------------------
# A1 — solve and verify
# ---------------------------------------------------------------------------

def solve(scenario: Scenario) -> Result:
    """
    Find the unique optimal assignment for the scenario.

    Returns Result with status collaboration | no_exchange | infeasible.
    Raises optimizer.errors.MissingBaselineError (→ 422) when an owner
    has no estimate for one of its own deliverable services.
    """
    return _solve(scenario)


def verify(scenario: Scenario, result: Result) -> Result:
    """
    Independently recompute the result from scenario + assignment list.
    Returns a fresh Result with a new certificate.
    Raises ValueError if the supplied assignments are inconsistent.

    Call this before passing computed values to Bedrock (/explain).
    Never trust client-supplied arithmetic.
    """
    return _verify(scenario, result)


# ---------------------------------------------------------------------------
# A2 — optimize alias and enumerate_assignments
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# A3 — validate_assignment and explain_infeasibility
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# A4 — compute_metrics and rank_allocation
# ---------------------------------------------------------------------------

def compute_metrics(
    scenario: Scenario,
    assignment_map: dict[str, str],
) -> tuple:
    """
    Compute per-business metrics and aggregate totals for a specific assignment map.

    Returns (list[BusinessMetrics] sorted by businessId, Totals).
    Raises MissingBaselineError if any owner lacks an estimate.
    Raises KeyError if any ID in assignment_map is unknown.

    All arithmetic is integer minutes.
    Labor burden is NOT monetary utility — savings are minutes of effort, not profit.
    """
    return _compute_metrics(scenario, assignment_map)


def rank_allocation(candidates: list) -> list[ScoredCandidate]:
    """
    Score and rank a list of (assignment_map, metrics, totals) candidates.

    Filters out candidates where any business has netSavingsMinutes < 0,
    even if the total net savings are positive.

    Remaining candidates sorted best-first by:
      1. Ascending total effortMinutes
      2. Ascending max effort/baseline ratio (integer cross-multiplication)
      3. Ascending lexicographic (deliverableId, providerId) pairs

    Returns list[ScoredCandidate], best first.  Empty list if none survive filter.
    """
    return _rank_allocation(candidates)


# ---------------------------------------------------------------------------
# Re-exports for backend convenience
# ---------------------------------------------------------------------------

__all__ = [
    "solve",
    "optimize",
    "enumerate_assignments",
    "verify",
    "validate_assignment",
    "explain_infeasibility",
    "AssignmentViolation",
    "InfeasibilityReport",
    "compute_metrics",
    "rank_allocation",
    "ScoredCandidate",
    "generate_certificate",
    "verify_certificate",
    "VerificationResult",
]


# ---------------------------------------------------------------------------
# A5 — generate_certificate and verify_certificate
# ---------------------------------------------------------------------------

def generate_certificate(scenario, assignments) -> "Certificate":
    """
    Generate a Certificate for a scenario + assignment list.

    Runs all constraint checks independently before generating.
    Raises ValueError if any check fails.

    Proves arithmetic correctness under supplied estimates and SHA-256 digest
    consistency.  Does NOT prove identity, quality equality, legal
    enforceability, profit, or solver optimality.
    """
    from contracts.models import Certificate  # noqa: F401 — for type annotation
    return _generate_certificate(scenario, assignments)


def verify_certificate(scenario, assignments, certificate) -> VerificationResult:
    """
    Independently verify a certificate against a scenario and assignment list.

    Reruns all checks from scratch and recomputes both digests.
    Returns VerificationResult with valid=True only when all checks pass
    AND both digests match.

    Tampered providers, wrong minutes, altered scenario or modified totals
    each produce a mismatch or check failure.
    """
    return _verify_certificate(scenario, assignments, certificate)
