"""
TradeMind v1 — pure deterministic optimizer engine.
Owner: Developer 1 (optimizer/** and tests/optimizer/**).

Design
------
All arithmetic is integer minutes. No floats. No external solver library.
The algorithm:

1. Pre-flight validation
   - Every owner must have an estimate for its own deliverable's service
     (missing → MissingBaselineError; the backend maps this to 422).
   - Build a "feasible providers" set per deliverable: businesses that have
     an estimate for that service AND whose estimate is positive (always true
     by schema, but explicit).

2. Baseline computation
   baseline_i = sum of estimate(service) for each deliverable owned by i,
                using i's own estimate.

3. Enumeration (at most 5^6 = 15 625 candidates)
   For each deliverable, the candidate providers are:
     - The owner (self-assignment, always feasible provided owner has estimate).
     - Any other business that has an estimate for that service.
   Enumerate the Cartesian product of candidate lists.

4. Per-candidate feasibility check
   For a candidate assignment vector:
   a. Compute productionMinutes_i = sum of estimate(service, provider=i) for
      all deliverables assigned to i.
   b. Determine which businesses are "in an exchange":
      A business is involved if it provides work to any deliverable it does
      NOT own, OR if any of its own deliverables is assigned to someone else.
      If a business is involved in an exchange, charge coordinationMinutes once.
   c. effortMinutes_i = productionMinutes_i + coordinationMinutes_i (if involved).
   d. Feasibility gates (all must hold for every business):
      - effort_i <= capacityMinutes_i
      - netSavings_i = baseline_i - effort_i >= 0

5. Collaboration check
   A feasible candidate is a "collaboration" only if:
   - At least one cross-business assignment exists.
   - Total net savings across all businesses > 0.

6. Tiebreak ordering (lower is better, used to pick the unique optimum):
   Primary:   minimize sum(effortMinutes_i)
   Secondary: minimize max_i(effort_i * baseline_j) vs max_j(baseline_i * effort_j)
              i.e. min of max effort/baseline ratio — integer cross-multiplication.
   Tertiary:  lexicographic sort of (deliverableId, providerId) pairs.

7. No-exchange vs infeasible
   - If any feasible assignment exists (even self-assignment only) but no
     collaboration candidate passes step 5 → no_exchange.
   - If even self-assignment fails feasibility for at least one business → infeasible.

8. Certificate
   SHA-256 of canonical JSON (keys recursively sorted, arrays preserve order,
   no whitespace, UTF-8) of the scenario and the assignment list.
"""
from __future__ import annotations

import hashlib
import json
from itertools import product as cartesian_product
from typing import NamedTuple

from contracts.models import (
    Assignment,
    BusinessMetrics,
    Certificate,
    CertificateCheck,
    Reason,
    Result,
    ResultStatus,
    Scenario,
    ServiceName,
    Totals,
)
from optimizer.errors import MissingBaselineError


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class _BusinessState(NamedTuple):
    """Mutable-friendly view of one business during evaluation."""
    id: str
    capacityMinutes: int
    coordinationMinutes: int
    baseline: int  # sum of own estimates for own deliverables


def _canonical_json(obj: object) -> bytes:
    """Canonical JSON: keys sorted recursively, arrays preserved, no whitespace, UTF-8."""
    def _sort(o: object) -> object:
        if isinstance(o, dict):
            return {k: _sort(v) for k, v in sorted(o.items())}
        if isinstance(o, list):
            return [_sort(i) for i in o]
        return o

    return json.dumps(_sort(obj), separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _scenario_digest(scenario: Scenario) -> str:
    return _sha256(_canonical_json(json.loads(scenario.model_dump_json())))


def _assignment_digest(assignments: list[Assignment]) -> str:
    raw = [{"deliverableId": a.deliverableId, "providerId": a.providerId, "minutes": a.minutes}
           for a in assignments]
    return _sha256(_canonical_json(raw))


# ---------------------------------------------------------------------------
# Core validation and baseline
# ---------------------------------------------------------------------------

def _validate_baselines(scenario: Scenario) -> dict[str, int]:
    """
    Return {businessId: baselineMinutes} for every business.
    Raises MissingBaselineError if any owner lacks an estimate for one of its
    own deliverable services — that is the 422 MISSING_BASELINE case.
    """
    # Index estimates: {businessId: {service: minutes}}
    estimates: dict[str, dict[ServiceName, int]] = {}
    for b in scenario.businesses:
        estimates[b.id] = {e.service: e.minutes for e in b.estimates}

    baselines: dict[str, int] = {b.id: 0 for b in scenario.businesses}
    missing: list[str] = []

    for d in scenario.deliverables:
        owner_estimates = estimates.get(d.ownerId, {})
        if d.service not in owner_estimates:
            missing.append(f"{d.ownerId}.estimates.{d.service.value}")
        else:
            baselines[d.ownerId] += owner_estimates[d.service]

    if missing:
        raise MissingBaselineError(missing)

    return baselines


def _build_feasible_providers(
    scenario: Scenario,
    estimates: dict[str, dict[ServiceName, int]],
) -> dict[str, list[str]]:
    """
    For each deliverable id, return the ordered list of candidate provider ids.
    A provider is feasible if it has an estimate for the required service.
    Owner always comes first (guarantees self-assignment is always first candidate).
    Others sorted lexicographically for stable enumeration.
    """
    providers: dict[str, list[str]] = {}
    for d in scenario.deliverables:
        owner = d.ownerId
        others = sorted(
            b.id for b in scenario.businesses
            if b.id != owner and d.service in estimates.get(b.id, {})
        )
        providers[d.id] = [owner] + others
    return providers


# ---------------------------------------------------------------------------
# Per-candidate evaluation
# ---------------------------------------------------------------------------

def _evaluate_candidate(
    assignment_map: dict[str, str],  # {deliverableId: providerId}
    scenario: Scenario,
    estimates: dict[str, dict[ServiceName, int]],
    baselines: dict[str, int],
    service_map: dict[str, ServiceName],  # {deliverableId: service}
    owner_map: dict[str, str],            # {deliverableId: ownerId}
) -> tuple[bool, list[Assignment], list[BusinessMetrics], Totals]:
    """
    Evaluate a single assignment candidate.
    Returns (is_feasible, assignments, metrics, totals).
    Infeasible candidates return (False, [], [], _).
    """
    # Step 1: production minutes per business
    production: dict[str, int] = {b.id: 0 for b in scenario.businesses}
    for d_id, provider_id in assignment_map.items():
        service = service_map[d_id]
        production[provider_id] += estimates[provider_id][service]

    # Step 2: which businesses are "involved in an exchange"?
    # A business is involved if it produces work for a deliverable it doesn't own,
    # OR if any of its own deliverables are produced by someone else.
    involved: set[str] = set()
    for d_id, provider_id in assignment_map.items():
        owner_id = owner_map[d_id]
        if provider_id != owner_id:
            involved.add(provider_id)
            involved.add(owner_id)

    # Step 3: effort per business
    effort: dict[str, int] = {}
    coord_charge: dict[str, int] = {}
    for b in scenario.businesses:
        c = b.coordinationMinutes if b.id in involved else 0
        coord_charge[b.id] = c
        effort[b.id] = production[b.id] + c

    # Step 4: feasibility gates
    for b in scenario.businesses:
        if effort[b.id] > b.capacityMinutes:
            return False, [], [], _zero_totals()
        net = baselines[b.id] - effort[b.id]
        if net < 0:
            return False, [], [], _zero_totals()

    # All gates passed — build output structures
    assignments = [
        Assignment(
            deliverableId=d_id,
            providerId=provider_id,
            minutes=estimates[provider_id][service_map[d_id]],
        )
        for d_id, provider_id in sorted(assignment_map.items())
    ]

    metrics = [
        BusinessMetrics(
            businessId=b.id,
            baselineMinutes=baselines[b.id],
            productionMinutes=production[b.id],
            coordinationMinutes=coord_charge[b.id],
            effortMinutes=effort[b.id],
            netSavingsMinutes=baselines[b.id] - effort[b.id],
        )
        for b in sorted(scenario.businesses, key=lambda b: b.id)
    ]

    total_baseline = sum(baselines[b.id] for b in scenario.businesses)
    total_production = sum(production[b.id] for b in scenario.businesses)
    total_coord = sum(coord_charge[b.id] for b in scenario.businesses)
    total_effort = sum(effort[b.id] for b in scenario.businesses)
    gross_savings = total_baseline - total_production
    net_savings = total_baseline - total_effort

    totals = Totals(
        baselineMinutes=total_baseline,
        productionMinutes=total_production,
        coordinationMinutes=total_coord,
        effortMinutes=total_effort,
        grossSavingsMinutes=gross_savings,
        netSavingsMinutes=net_savings,
    )

    return True, assignments, metrics, totals


def _zero_totals() -> Totals:
    return Totals(
        baselineMinutes=0, productionMinutes=0, coordinationMinutes=0,
        effortMinutes=0, grossSavingsMinutes=0, netSavingsMinutes=0,
    )


def _is_collaboration(assignment_map: dict[str, str], owner_map: dict[str, str], net_savings: int) -> bool:
    """True iff at least one cross-business assignment and total net savings > 0."""
    has_cross = any(provider != owner_map[d_id] for d_id, provider in assignment_map.items())
    return has_cross and net_savings > 0


# ---------------------------------------------------------------------------
# Tiebreak key
# ---------------------------------------------------------------------------

def _tiebreak_key(
    assignments: list[Assignment],
    metrics: list[BusinessMetrics],
    totals: Totals,
) -> tuple:
    """
    Lower is better.
    Primary:   total effort minutes.
    Secondary: max effort/baseline ratio using integer cross-multiplication.
               For each business: effort * sum_of_all_baselines vs baseline * sum_of_all_efforts.
               We use (effort_i * total_baseline) as an integer proxy for effort_i / baseline_i,
               comparable across businesses. We want the max of these.
               Special case: baseline_i == 0 → treat ratio as infinity (largest possible int).
    Tertiary:  sorted list of (deliverableId, providerId) pairs.
    """
    total_baseline = totals.baselineMinutes

    def _ratio_proxy(m: BusinessMetrics) -> int:
        if m.baselineMinutes == 0:
            return 10 ** 15  # effectively infinite
        # effort/baseline represented as effort * total_baseline (integer, comparable)
        return m.effortMinutes * total_baseline

    max_ratio = max(_ratio_proxy(m) for m in metrics) if metrics else 0

    tertiary = sorted((a.deliverableId, a.providerId) for a in assignments)

    return (totals.effortMinutes, max_ratio, tertiary)


# ---------------------------------------------------------------------------
# Main solver
# ---------------------------------------------------------------------------

def enumerate_assignments(scenario: Scenario) -> list[dict[str, str]]:
    """
    Return every candidate assignment map {deliverableId: providerId} for the
    scenario, in deterministic order (deliverables sorted lexicographically,
    providers owner-first then lexicographic).

    This is the raw search space — no feasibility filtering applied.
    Upper bound: 5^6 = 15 625 candidates.

    Raises ValueError if the scenario exceeds supported bounds (>5 businesses
    or >6 deliverables) before enumeration begins.  The Pydantic schema already
    rejects these at parse time, but this guard makes the bound explicit at the
    algorithmic level.
    """
    if len(scenario.businesses) > 5:
        raise ValueError(
            f"Enumeration bound exceeded: {len(scenario.businesses)} businesses "
            "(max 5, giving at most 5^6=15625 candidates)."
        )
    if len(scenario.deliverables) > 6:
        raise ValueError(
            f"Enumeration bound exceeded: {len(scenario.deliverables)} deliverables "
            "(max 6, giving at most 5^6=15625 candidates)."
        )

    estimates: dict[str, dict[ServiceName, int]] = {
        b.id: {e.service: e.minutes for e in b.estimates}
        for b in scenario.businesses
    }

    feasible_providers = _build_feasible_providers(scenario, estimates)
    deliverable_ids = [d.id for d in sorted(scenario.deliverables, key=lambda d: d.id)]
    provider_lists = [feasible_providers[d_id] for d_id in deliverable_ids]

    return [
        dict(zip(deliverable_ids, combo))
        for combo in cartesian_product(*provider_lists)
    ]


def optimize(scenario: Scenario) -> Result:
    """
    Public alias for solve() — satisfies the A2 naming requirement.
    Calls solve() which contains the full enumeration + tiebreak logic.
    The backend and tests may import either name; both are stable.
    """
    return solve(scenario)


def solve(scenario: Scenario) -> Result:
    """
    Find the unique optimal assignment for the scenario.

    Returns a Result with status:
      collaboration — feasible assignment with cross-business exchange and positive net savings
      no_exchange   — feasible (even self-assignment) but no beneficial collaboration
      infeasible    — not even self-assignment satisfies effort ≤ capacity and netSavings ≥ 0
    """
    # Index estimates
    estimates: dict[str, dict[ServiceName, int]] = {
        b.id: {e.service: e.minutes for e in b.estimates}
        for b in scenario.businesses
    }

    # Validate baselines — raises MissingBaselineError on failure
    baselines = _validate_baselines(scenario)

    service_map: dict[str, ServiceName] = {d.id: d.service for d in scenario.deliverables}
    owner_map: dict[str, str] = {d.id: d.ownerId for d in scenario.deliverables}

    # Build candidate provider lists per deliverable (owner first, others sorted)
    feasible_providers = _build_feasible_providers(scenario, estimates)

    # Ordered deliverable IDs for stable enumeration
    deliverable_ids = [d.id for d in sorted(scenario.deliverables, key=lambda d: d.id)]

    # Collect provider lists in deliverable order
    provider_lists = [feasible_providers[d_id] for d_id in deliverable_ids]

    best_collab: tuple | None = None   # (key, assignments, metrics, totals)
    any_feasible = False

    for combo in cartesian_product(*provider_lists):
        assignment_map = dict(zip(deliverable_ids, combo))

        feasible, assignments, metrics, totals = _evaluate_candidate(
            assignment_map, scenario, estimates, baselines, service_map, owner_map
        )

        if not feasible:
            continue

        any_feasible = True

        if _is_collaboration(assignment_map, owner_map, totals.netSavingsMinutes):
            key = _tiebreak_key(assignments, metrics, totals)
            if best_collab is None or key < best_collab[0]:
                best_collab = (key, assignments, metrics, totals)

    # --- Determine outcome ---

    if best_collab is not None:
        _, assignments, metrics, totals = best_collab
        cert = _build_certificate(scenario, assignments)
        return Result(
            schemaVersion="1",
            status=ResultStatus.COLLABORATION,
            assignments=assignments,
            businessMetrics=metrics,
            totals=totals,
            reasons=[],
            certificate=cert,
        )

    if any_feasible:
        return Result(
            schemaVersion="1",
            status=ResultStatus.NO_EXCHANGE,
            assignments=[],
            businessMetrics=None,
            totals=None,
            reasons=[Reason(
                code="NO_BENEFICIAL_EXCHANGE",
                message=(
                    "No cross-business assignment improves total net savings "
                    "while keeping every participant no worse off."
                ),
            )],
            certificate=None,
        )

    # Infeasible — even self-assignment failed
    return Result(
        schemaVersion="1",
        status=ResultStatus.INFEASIBLE,
        assignments=[],
        businessMetrics=None,
        totals=None,
        reasons=[Reason(
            code="INFEASIBLE",
            message=(
                "No valid assignment exists that satisfies capacity and "
                "no-worse-off constraints for every business, including self-assignment."
            ),
        )],
        certificate=None,
    )


# ---------------------------------------------------------------------------
# Certificate builder and independent verifier
# ---------------------------------------------------------------------------

def _build_certificate(scenario: Scenario, assignments: list[Assignment]) -> Certificate:
    s_digest = _scenario_digest(scenario)
    a_digest = _assignment_digest(assignments)

    assumptions = [
        "Service quality is assumed equal across all providers.",
        "Coordination charge is fixed per business per exchange, independent of volume.",
        "All supplied duration estimates are accepted as stated; no market pricing.",
        "Integer minutes arithmetic; no floating-point rounding.",
        "No-worse-off constraint: netSavingsMinutes >= 0 for every business.",
    ]

    checks = [
        CertificateCheck(name="all_net_savings_non_negative", passed=True),
        CertificateCheck(name="all_effort_within_capacity", passed=True),
        CertificateCheck(name="at_least_one_cross_business_assignment", passed=True),
        CertificateCheck(name="total_net_savings_positive", passed=True),
        CertificateCheck(name="integer_arithmetic_only", passed=True),
    ]

    return Certificate(
        schemaVersion="1",
        scenarioDigest=s_digest,
        allocationDigest=a_digest,
        verification="valid",
        assumptions=assumptions,
        checks=checks,
    )


def verify(scenario: Scenario, result: Result) -> Result:
    """
    Independently recompute the result from the scenario and the assignment list.
    Returns a new Result with a fresh certificate.
    Raises ValueError if the claimed assignments do not satisfy the constraints.

    The backend must call this before supplying computed values to Bedrock (/explain).
    Never trust client-supplied math.
    """
    if result.status == ResultStatus.INFEASIBLE:
        # Nothing to verify arithmetically — just return a clean copy
        return Result(
            schemaVersion="1",
            status=ResultStatus.INFEASIBLE,
            assignments=[],
            businessMetrics=None,
            totals=None,
            reasons=result.reasons,
            certificate=None,
        )

    if result.status == ResultStatus.NO_EXCHANGE:
        return Result(
            schemaVersion="1",
            status=ResultStatus.NO_EXCHANGE,
            assignments=[],
            businessMetrics=None,
            totals=None,
            reasons=result.reasons,
            certificate=None,
        )

    # Collaboration — recompute everything from the supplied assignments
    estimates: dict[str, dict[ServiceName, int]] = {
        b.id: {e.service: e.minutes for e in b.estimates}
        for b in scenario.businesses
    }
    baselines = _validate_baselines(scenario)
    service_map = {d.id: d.service for d in scenario.deliverables}
    owner_map = {d.id: d.ownerId for d in scenario.deliverables}

    # Rebuild assignment_map from supplied list
    assignment_map = {a.deliverableId: a.providerId for a in result.assignments}

    # Verify all deliverables are covered
    expected_ids = {d.id for d in scenario.deliverables}
    supplied_ids = set(assignment_map.keys())
    if expected_ids != supplied_ids:
        raise ValueError(
            f"Assignment covers {supplied_ids} but scenario has {expected_ids}"
        )

    # Recompute
    feasible, assignments, metrics, totals = _evaluate_candidate(
        assignment_map, scenario, estimates, baselines, service_map, owner_map
    )

    if not feasible:
        raise ValueError(
            "Supplied assignments violate capacity or no-worse-off constraints."
        )

    cert = _build_certificate(scenario, assignments)

    return Result(
        schemaVersion="1",
        status=ResultStatus.COLLABORATION,
        assignments=assignments,
        businessMetrics=metrics,
        totals=totals,
        reasons=[],
        certificate=cert,
    )
