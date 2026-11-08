"""
TradeMind v1 — feasibility and capacity verification.
Owner: Developer 1 (optimizer/** and tests/optimizer/**).

Public surface (used by engine and backend):
  validate_assignment(scenario, assignments) → list[AssignmentViolation]
  explain_infeasibility(scenario)            → InfeasibilityReport

Design
------
validate_assignment
  Checks a fully specified assignment list against a confirmed scenario.
  Returns a list of violation objects (empty = valid).  Does NOT raise —
  the caller decides whether to raise or include violations in a response.

  Checks in order (each independent — all violations reported, not just first):
    1. Coverage       — exactly one assignment per deliverable, no extras.
    2. Known IDs      — every deliverableId and providerId references a real entity.
    3. Service compat — provider must have an estimate for the deliverable's service.
    4. Duration       — assignment.minutes must equal the provider's estimate exactly.
    5. Capacity       — for each business: effortMinutes <= capacityMinutes.
    6. No-worse-off   — for each business: netSavingsMinutes >= 0.

  The caller must have validated baselines (MissingBaselineError) before calling
  validate_assignment — this function assumes baselines can be computed.

explain_infeasibility
  Returns a structured report describing which constraints make every possible
  assignment infeasible.  Used by the backend /explain route and for diagnostics.

  Reports three independent categories:
    capacity_violations  — businesses whose self-assignment already exceeds capacity.
    savings_violations   — businesses whose self-assignment already harms them
                           (netSavings < 0), even before coordination.
    unsupported_services — deliverables whose owner has no estimate for their service
                           (these become MissingBaselineError on /optimize calls).

  Important: this function reports OBSERVED constraints; it does NOT claim to compute
  a minimal unsatisfiable core (MUC/IIS).  Judges should not be told it finds MUCs.

All arithmetic is integer minutes.  No floats.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from optimizer.contract import (
    Assignment,
    Scenario,
    ServiceName,
)
from optimizer.errors import MissingBaselineError


# ---------------------------------------------------------------------------
# Violation types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AssignmentViolation:
    """A single constraint violation found in an assignment list."""
    kind: str        # one of the KIND_* constants below
    message: str
    fields: list[str] = field(default_factory=list)

    # Violation kind constants
    KIND_MISSING_DELIVERABLE  = "MISSING_DELIVERABLE"    # no assignment for a deliverable
    KIND_DUPLICATE_DELIVERABLE = "DUPLICATE_DELIVERABLE" # more than one assignment for same deliverable
    KIND_UNKNOWN_DELIVERABLE  = "UNKNOWN_DELIVERABLE"    # deliverableId not in scenario
    KIND_UNKNOWN_PROVIDER     = "UNKNOWN_PROVIDER"       # providerId not in scenario
    KIND_INCOMPATIBLE_SERVICE = "INCOMPATIBLE_SERVICE"   # provider has no estimate for the service
    KIND_WRONG_DURATION       = "WRONG_DURATION"         # minutes != provider's estimate
    KIND_CAPACITY_EXCEEDED    = "CAPACITY_EXCEEDED"      # effort > capacityMinutes
    KIND_SAVINGS_NEGATIVE     = "SAVINGS_NEGATIVE"       # netSavingsMinutes < 0


# ---------------------------------------------------------------------------
# Infeasibility report
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CapacityViolation:
    businessId: str
    effortMinutes: int
    capacityMinutes: int
    # effort - capacity (positive = excess)
    excessMinutes: int


@dataclass(frozen=True)
class SavingsViolation:
    businessId: str
    baselineMinutes: int
    effortMinutes: int
    netSavingsMinutes: int   # negative value


@dataclass(frozen=True)
class UnsupportedService:
    businessId: str
    deliverableId: str
    service: str


@dataclass
class InfeasibilityReport:
    """
    Structured report of observed infeasibility reasons for a scenario.

    This describes OBSERVED constraints; it is NOT a minimal unsatisfiable
    core (MUC) and should not be presented as one.
    """
    # Businesses whose self-assignment already exceeds their capacity
    capacity_violations: list[CapacityViolation] = field(default_factory=list)
    # Businesses whose self-assignment already harms them (netSavings < 0)
    savings_violations: list[SavingsViolation] = field(default_factory=list)
    # Deliverables whose owner lacks an estimate (MISSING_BASELINE territory)
    unsupported_services: list[UnsupportedService] = field(default_factory=list)

    @property
    def is_infeasible(self) -> bool:
        """True if any category has at least one entry."""
        return bool(
            self.capacity_violations
            or self.savings_violations
            or self.unsupported_services
        )

    def summary(self) -> str:
        """Human-readable single-line summary for logging and /explain context."""
        parts: list[str] = []
        if self.capacity_violations:
            ids = ", ".join(v.businessId for v in self.capacity_violations)
            parts.append(f"capacity exceeded for: {ids}")
        if self.savings_violations:
            ids = ", ".join(v.businessId for v in self.savings_violations)
            parts.append(f"self-assignment harms: {ids}")
        if self.unsupported_services:
            ids = ", ".join(f"{u.businessId}.{u.service}" for u in self.unsupported_services)
            parts.append(f"missing baseline estimates: {ids}")
        if not parts:
            return "No infeasibility detected in self-assignment analysis."
        return "Infeasibility observed: " + "; ".join(parts) + "."


# ---------------------------------------------------------------------------
# validate_assignment
# ---------------------------------------------------------------------------

def validate_assignment(
    scenario: Scenario,
    assignments: list[Assignment],
) -> list[AssignmentViolation]:
    """
    Validate a fully specified assignment list against a confirmed scenario.

    Returns a (possibly empty) list of AssignmentViolation objects.
    Empty list means the assignment is valid.
    Does NOT raise — the caller decides how to handle violations.

    Prerequisite: the caller must ensure baselines are computable (i.e.
    _validate_baselines has been called or equivalent checks done).
    This function will still detect missing estimates via KIND_INCOMPATIBLE_SERVICE
    for non-owner providers, but missing owner estimates are a separate concern
    (MissingBaselineError from the solver).
    """
    violations: list[AssignmentViolation] = []

    # Index scenario entities
    business_ids: set[str] = {b.id for b in scenario.businesses}
    deliverable_ids: set[str] = {d.id for d in scenario.deliverables}
    service_map: dict[str, ServiceName] = {d.id: d.service for d in scenario.deliverables}
    owner_map: dict[str, str] = {d.id: d.ownerId for d in scenario.deliverables}
    estimates: dict[str, dict[ServiceName, int]] = {
        b.id: {e.service: e.minutes for e in b.estimates}
        for b in scenario.businesses
    }

    # ---- 1. Check for unknown deliverable/provider IDs first ----------------
    seen_deliverables: dict[str, list[Assignment]] = {}
    for a in assignments:
        if a.deliverableId not in deliverable_ids:
            violations.append(AssignmentViolation(
                kind=AssignmentViolation.KIND_UNKNOWN_DELIVERABLE,
                message=f"Deliverable {a.deliverableId!r} is not in the scenario.",
                fields=[a.deliverableId],
            ))
        else:
            seen_deliverables.setdefault(a.deliverableId, []).append(a)

        if a.providerId not in business_ids:
            violations.append(AssignmentViolation(
                kind=AssignmentViolation.KIND_UNKNOWN_PROVIDER,
                message=f"Provider {a.providerId!r} is not a business in the scenario.",
                fields=[a.providerId],
            ))

    # ---- 2. Coverage — missing and duplicate --------------------------------
    for d_id in deliverable_ids:
        asgns = seen_deliverables.get(d_id, [])
        if not asgns:
            violations.append(AssignmentViolation(
                kind=AssignmentViolation.KIND_MISSING_DELIVERABLE,
                message=f"No assignment provided for deliverable {d_id!r}.",
                fields=[d_id],
            ))
        elif len(asgns) > 1:
            violations.append(AssignmentViolation(
                kind=AssignmentViolation.KIND_DUPLICATE_DELIVERABLE,
                message=(
                    f"Deliverable {d_id!r} has {len(asgns)} assignments; "
                    "exactly one is required."
                ),
                fields=[d_id],
            ))

    # ---- 3. Per-assignment: service compat and duration ----------------------
    for a in assignments:
        if a.deliverableId not in deliverable_ids or a.providerId not in business_ids:
            continue  # already flagged above; skip dependent checks

        svc = service_map[a.deliverableId]
        provider_estimates = estimates.get(a.providerId, {})

        if svc not in provider_estimates:
            violations.append(AssignmentViolation(
                kind=AssignmentViolation.KIND_INCOMPATIBLE_SERVICE,
                message=(
                    f"Provider {a.providerId!r} has no estimate for service "
                    f"{svc.value!r} required by deliverable {a.deliverableId!r}."
                ),
                fields=[a.deliverableId, a.providerId],
            ))
        else:
            expected_minutes = provider_estimates[svc]
            if a.minutes != expected_minutes:
                violations.append(AssignmentViolation(
                    kind=AssignmentViolation.KIND_WRONG_DURATION,
                    message=(
                        f"Assignment for deliverable {a.deliverableId!r} to "
                        f"{a.providerId!r} claims {a.minutes} minutes, but "
                        f"{a.providerId!r}'s estimate for {svc.value!r} is "
                        f"{expected_minutes} minutes."
                    ),
                    fields=[a.deliverableId, a.providerId],
                ))

    # ---- 4. Capacity and savings checks (only if coverage is complete) -------
    # Build a clean assignment map (only valid single-assignment deliverables)
    clean_map: dict[str, str] = {}
    for d_id, asgns in seen_deliverables.items():
        if len(asgns) == 1 and d_id in deliverable_ids and asgns[0].providerId in business_ids:
            svc = service_map[d_id]
            if svc in estimates.get(asgns[0].providerId, {}):
                clean_map[d_id] = asgns[0].providerId

    if len(clean_map) == len(deliverable_ids):
        # Only run capacity/savings checks when we have a complete valid coverage
        production: dict[str, int] = {b.id: 0 for b in scenario.businesses}
        for d_id, prov_id in clean_map.items():
            svc = service_map[d_id]
            production[prov_id] += estimates[prov_id][svc]

        involved: set[str] = set()
        for d_id, prov_id in clean_map.items():
            if prov_id != owner_map[d_id]:
                involved.add(prov_id)
                involved.add(owner_map[d_id])

        # Compute baselines (may raise MissingBaselineError — propagate to caller)
        baselines = _compute_baselines(scenario)

        for b in scenario.businesses:
            coord = b.coordinationMinutes if b.id in involved else 0
            effort = production[b.id] + coord
            capacity = b.capacityMinutes

            if effort > capacity:
                violations.append(AssignmentViolation(
                    kind=AssignmentViolation.KIND_CAPACITY_EXCEEDED,
                    message=(
                        f"Business {b.id!r}: effort={effort} min exceeds "
                        f"capacity={capacity} min (production={production[b.id]}, "
                        f"coordination={coord})."
                    ),
                    fields=[b.id],
                ))

            baseline = baselines.get(b.id, 0)
            net_savings = baseline - effort
            if net_savings < 0:
                violations.append(AssignmentViolation(
                    kind=AssignmentViolation.KIND_SAVINGS_NEGATIVE,
                    message=(
                        f"Business {b.id!r}: netSavings={net_savings} min "
                        f"(baseline={baseline}, effort={effort}). "
                        "This business would be worse off."
                    ),
                    fields=[b.id],
                ))

    return violations


def _compute_baselines(scenario: Scenario) -> dict[str, int]:
    """
    Compute baseline minutes per business.
    Raises MissingBaselineError if any owner lacks an estimate for its deliverable service.
    Shared logic extracted here so feasibility.py doesn't need to import from engine.
    """
    estimates: dict[str, dict[ServiceName, int]] = {
        b.id: {e.service: e.minutes for e in b.estimates}
        for b in scenario.businesses
    }
    baselines: dict[str, int] = {b.id: 0 for b in scenario.businesses}
    missing: list[str] = []

    for d in scenario.deliverables:
        owner_ests = estimates.get(d.ownerId, {})
        if d.service not in owner_ests:
            missing.append(f"{d.ownerId}.estimates.{d.service.value}")
        else:
            baselines[d.ownerId] += owner_ests[d.service]

    if missing:
        raise MissingBaselineError(missing)

    return baselines


# ---------------------------------------------------------------------------
# explain_infeasibility
# ---------------------------------------------------------------------------

def explain_infeasibility(scenario: Scenario) -> InfeasibilityReport:
    """
    Analyse a scenario and return a structured report of observed infeasibility
    reasons, based on self-assignment analysis.

    This function checks each business's self-assignment (owner performs its own work).
    If self-assignment is infeasible for any business, the scenario is globally infeasible
    (since every feasible allocation must include at least self-assignment as a baseline).

    Does NOT compute a minimal unsatisfiable core.  The report lists every observed
    constraint violation — there may be redundancy.

    Called by:
      - The solver to produce the 'reasons' list in an INFEASIBLE Result.
      - The backend /explain route to enrich the Bedrock prompt with truthful diagnostics.
    """
    report = InfeasibilityReport()

    estimates: dict[str, dict[ServiceName, int]] = {
        b.id: {e.service: e.minutes for e in b.estimates}
        for b in scenario.businesses
    }

    # Check for missing baseline estimates (these block even self-assignment)
    for d in scenario.deliverables:
        owner_ests = estimates.get(d.ownerId, {})
        if d.service not in owner_ests:
            report.unsupported_services.append(UnsupportedService(
                businessId=d.ownerId,
                deliverableId=d.id,
                service=d.service.value,
            ))

    # If any baseline is missing we can't compute effort — return early
    if report.unsupported_services:
        return report

    # Compute baselines (safe now — no missing estimates)
    baselines: dict[str, int] = {b.id: 0 for b in scenario.businesses}
    for d in scenario.deliverables:
        baselines[d.ownerId] += estimates[d.ownerId][d.service]

    # Check self-assignment feasibility for each business
    # Self-assignment: every business performs all its own deliverables itself.
    # No cross-business assignments → no coordination charges.
    self_production: dict[str, int] = {b.id: 0 for b in scenario.businesses}
    for d in scenario.deliverables:
        svc = d.service
        self_production[d.ownerId] += estimates[d.ownerId][svc]

    for b in scenario.businesses:
        effort = self_production[b.id]   # no coordination in pure self-assignment
        capacity = b.capacityMinutes
        baseline = baselines[b.id]
        net_savings = baseline - effort

        if effort > capacity:
            report.capacity_violations.append(CapacityViolation(
                businessId=b.id,
                effortMinutes=effort,
                capacityMinutes=capacity,
                excessMinutes=effort - capacity,
            ))

        if net_savings < 0:
            report.savings_violations.append(SavingsViolation(
                businessId=b.id,
                baselineMinutes=baseline,
                effortMinutes=effort,
                netSavingsMinutes=net_savings,
            ))

    return report
