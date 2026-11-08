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
from optimizer.contract import Result, Scenario

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
from optimizer.simulation import (
    ResultDiff,
    SimulationError,
    apply_changes as _apply_changes,
    diff_results as _diff_results,
)
from optimizer.explain import (
    BottleneckReport,
    SensitivityReport,
    TestedCandidate,
    analyze_bottlenecks as _analyze_bottlenecks,
    sensitivity_report as _sensitivity_report,
)
from optimizer.models import (
    DEFAULT_CONFIG,
    ExtendedModeError,
    OptimizerConfig,
    candidate_count_upper_bound,
    validate_scenario_bounds,
)
from optimizer.risk import (
    EstimatePerturbation,
    PerturbationOutcome,
    PerturbationScenario,
    RiskReport,
    evaluate_risk as _evaluate_risk,
)


# ---------------------------------------------------------------------------
# A1 — solve and verify
# ---------------------------------------------------------------------------

def solve(scenario: Scenario, config=None) -> Result:
    """
    Find the unique optimal assignment for the scenario.

    Returns Result with status collaboration | no_exchange | infeasible.
    Raises optimizer.errors.MissingBaselineError (→ 422) when an owner
    has no estimate for one of its own deliverable services.

    config (A9): optional OptimizerConfig to gate extended 4/5-business mode.
    When None, historical behaviour is preserved.
    """
    return _solve(scenario, config)


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

def optimize(scenario: Scenario, config=None) -> Result:
    """
    Alias for solve() — A2 public entrypoint name used by the backend.
    Identical behaviour; either name is stable for B4 integration.

    config (A9): optional OptimizerConfig to gate extended 4/5-business mode.
    """
    return _optimize(scenario, config)


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
    "apply_changes",
    "diff_results",
    "SimulationError",
    "ResultDiff",
    "analyze_bottlenecks",
    "sensitivity_report",
    "BottleneckReport",
    "SensitivityReport",
    "TestedCandidate",
    "OptimizerConfig",
    "DEFAULT_CONFIG",
    "ExtendedModeError",
    "validate_scenario_bounds",
    "candidate_count_upper_bound",
    "evaluate_risk",
    "EstimatePerturbation",
    "PerturbationScenario",
    "PerturbationOutcome",
    "RiskReport",
]


# ---------------------------------------------------------------------------
# A10 — evaluate_risk
# ---------------------------------------------------------------------------

def evaluate_risk(scenario, perturbations) -> RiskReport:
    """
    Evaluate scenario sensitivity to a finite, explicit set of estimate
    perturbations by rerunning the solver on each.

    Preserves the certified nominal result unchanged. Reports the worst
    OBSERVED outcome across the tested perturbations. Makes NO probabilistic
    claims — this is sensitivity analysis, not probability.
    """
    return _evaluate_risk(scenario, perturbations)


# ---------------------------------------------------------------------------
# A8 — analyze_bottlenecks and sensitivity_report
# ---------------------------------------------------------------------------

def analyze_bottlenecks(scenario: Scenario) -> BottleneckReport:
    """
    Report observed binding constraints for a scenario.

    Runs the solver once, then inspects self-assignment feasibility of each
    business.  Reports every observed binding constraint and any missing
    baseline estimates.  Does NOT claim a single root cause or minimal repair.
    """
    return _analyze_bottlenecks(scenario)


def sensitivity_report(scenario, candidates) -> SensitivityReport:
    """
    Test a finite, explicit list of (label, changes) candidates by actually
    rerunning the solver on each.

    repaired=True only when a rerun produced 'collaboration'.
    Makes NO claim about untested changes or global minimum repairs.
    """
    return _sensitivity_report(scenario, candidates)


# ---------------------------------------------------------------------------
# A6 — apply_changes and diff_results
# ---------------------------------------------------------------------------

def apply_changes(scenario, changes) -> Scenario:
    """
    Apply a validated list of Change objects to a deep copy of the scenario.

    The original scenario is NEVER mutated.
    Raises SimulationError (subclass of ValueError) atomically if any
    validation fails — duplicate targets, unknown IDs, or bad values.

    Allowed paths: capacity, coordination, estimate (service minutes).
    No language parsing — caller must supply explicit Change objects.
    After applying, the patched scenario is re-validated through Pydantic.
    """
    return _apply_changes(scenario, changes)


def diff_results(before: Result, after: Result) -> ResultDiff:
    """
    Compare two Results and return a ResultDiff with per-business deltas
    and aggregate totals deltas.

    Does NOT mutate either Result and does NOT change the Result wire schema.
    Used by the backend /simulate handler and the UI to display before/after.
    """
    return _diff_results(before, after)


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
    from optimizer.contract import Certificate  # noqa: F401 — for type annotation
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
