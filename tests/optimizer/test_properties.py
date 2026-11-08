"""
A7 — Property-based and regression coverage (High priority, skippable enhancement).
Owner: Developer 1.

Uses Hypothesis to generate small valid scenarios and assert the solver's
invariants hold across a wide input space. Includes an independent brute-force
oracle (coded separately from engine.py) for optimality cross-checking.

Core contract tests already exist in test_contract.py, test_solver.py,
test_feasibility.py, test_metrics.py, test_certificate.py, test_simulation.py.
This file is additive regression/fuzz coverage — it does not replace them.

Recorded seed: HYPOTHESIS_SEED below is set via settings(derandomize=True) so
failures are deterministic and minimal examples are preserved by Hypothesis's
.hypothesis database.

Run: python -m pytest tests/optimizer/test_properties.py -q
"""
from __future__ import annotations

from itertools import product as cartesian_product

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from contracts.models import (
    Assignment,
    Result,
    ResultStatus,
    Scenario,
    ServiceName,
)
from optimizer.errors import MissingBaselineError
from optimizer.solver import optimize, verify, verify_certificate, apply_changes


# ---------------------------------------------------------------------------
# Hypothesis settings — bounded, deterministic
# ---------------------------------------------------------------------------

# derandomize=True makes the suite reproducible run-to-run (recorded behaviour).
_SETTINGS = settings(
    max_examples=150,
    deadline=None,  # solver is fast but CI timing varies
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

SERVICES = ["coding", "design", "writing"]


# ---------------------------------------------------------------------------
# Scenario generator
# ---------------------------------------------------------------------------

@st.composite
def valid_scenarios(draw, min_businesses=3, max_businesses=3, max_deliverables=3):
    """
    Generate a valid 3-business scenario where every owner has an estimate for
    its own deliverable's service (so MISSING_BASELINE never fires here —
    that path is tested separately).

    Keeps numbers small and bounded for fast exhaustive-oracle comparison.
    """
    n_biz = draw(st.integers(min_value=min_businesses, max_value=max_businesses))
    business_ids = [f"b{i}" for i in range(n_biz)]

    # Each business gets estimates for a random non-empty subset of services.
    # We'll ensure each owner can serve its own deliverable below.
    minutes_strategy = st.integers(min_value=30, max_value=300)
    capacity_strategy = st.integers(min_value=0, max_value=900)
    coord_strategy = st.integers(min_value=0, max_value=200)

    # Decide deliverables first: each deliverable owned by some business, with a service.
    n_del = draw(st.integers(min_value=1, max_value=max_deliverables))
    deliverables = []
    for j in range(n_del):
        owner = draw(st.sampled_from(business_ids))
        service = draw(st.sampled_from(SERVICES))
        deliverables.append({"id": f"d{j}", "ownerId": owner, "service": service})

    # For each business, collect the services it MUST support (its own deliverables)
    required: dict[str, set[str]] = {bid: set() for bid in business_ids}
    for d in deliverables:
        required[d["ownerId"]].add(d["service"])

    businesses = []
    for bid in business_ids:
        # Services this business supports: its required ones plus a random subset
        supported = set(required[bid])
        for svc in SERVICES:
            if svc not in supported and draw(st.booleans()):
                supported.add(svc)
        # Must support at least its required services (guaranteed above)
        estimates = [
            {"service": svc, "minutes": draw(minutes_strategy)}
            for svc in sorted(supported)
        ]
        businesses.append({
            "id": bid,
            "name": bid.upper(),
            "capacityMinutes": draw(capacity_strategy),
            "coordinationMinutes": draw(coord_strategy),
            "estimates": estimates,
        })

    return {
        "schemaVersion": "1",
        "businesses": businesses,
        "deliverables": deliverables,
        "confirmed": True,
    }


# ---------------------------------------------------------------------------
# Independent brute-force oracle (NOT shared with engine.py)
# ---------------------------------------------------------------------------

def _oracle_best(scenario: Scenario):
    """
    Independent exhaustive search. Returns (status, best_assignment_map, total_effort)
    where status is one of 'collaboration' | 'no_exchange' | 'infeasible'.
    Coded separately from the engine for cross-checking.
    """
    estimates = {b.id: {e.service: e.minutes for e in b.estimates} for b in scenario.businesses}
    owner_map = {d.id: d.ownerId for d in scenario.deliverables}
    service_map = {d.id: d.service for d in scenario.deliverables}

    baselines = {b.id: 0 for b in scenario.businesses}
    for d in scenario.deliverables:
        baselines[d.ownerId] += estimates[d.ownerId][d.service]

    d_ids = sorted(d.id for d in scenario.deliverables)
    provider_lists = []
    for d_id in d_ids:
        svc = service_map[d_id]
        owner = owner_map[d_id]
        cands = [owner] + sorted(
            b.id for b in scenario.businesses
            if b.id != owner and svc in estimates.get(b.id, {})
        )
        provider_lists.append(cands)

    any_feasible = False
    best_collab_key = None
    best_collab_effort = None

    cap = {b.id: b.capacityMinutes for b in scenario.businesses}
    coord = {b.id: b.coordinationMinutes for b in scenario.businesses}
    total_baseline = sum(baselines.values())

    for combo in cartesian_product(*provider_lists):
        amap = dict(zip(d_ids, combo))
        prod = {b.id: 0 for b in scenario.businesses}
        for d_id, prov in amap.items():
            prod[prov] += estimates[prov][service_map[d_id]]

        involved = set()
        for d_id, prov in amap.items():
            if prov != owner_map[d_id]:
                involved.add(prov)
                involved.add(owner_map[d_id])

        effort = {b.id: prod[b.id] + (coord[b.id] if b.id in involved else 0)
                  for b in scenario.businesses}

        if any(effort[b.id] > cap[b.id] for b in scenario.businesses):
            continue
        if any(baselines[b.id] - effort[b.id] < 0 for b in scenario.businesses):
            continue

        any_feasible = True
        has_cross = any(prov != owner_map[d_id] for d_id, prov in amap.items())
        total_net = sum(baselines[b.id] - effort[b.id] for b in scenario.businesses)
        if not (has_cross and total_net > 0):
            continue

        total_effort = sum(effort.values())

        def _ratio(bid):
            if baselines[bid] == 0:
                return 10 ** 15
            return effort[bid] * total_baseline

        max_ratio = max(_ratio(b.id) for b in scenario.businesses)
        key = (total_effort, max_ratio, sorted(amap.items()))
        if best_collab_key is None or key < best_collab_key:
            best_collab_key = key
            best_collab_effort = total_effort

    if best_collab_key is not None:
        return "collaboration", best_collab_effort
    if any_feasible:
        return "no_exchange", None
    return "infeasible", None


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------

class TestSolverInvariants:

    @_SETTINGS
    @given(valid_scenarios())
    def test_coverage_all_deliverables_assigned(self, raw):
        scenario = Scenario.model_validate(raw)
        result = optimize(scenario)
        if result.status == ResultStatus.COLLABORATION:
            assigned = {a.deliverableId for a in result.assignments}
            expected = {d.id for d in scenario.deliverables}
            assert assigned == expected

    @_SETTINGS
    @given(valid_scenarios())
    def test_assignment_minutes_equal_provider_estimate(self, raw):
        scenario = Scenario.model_validate(raw)
        result = optimize(scenario)
        if result.status == ResultStatus.COLLABORATION:
            est = {b.id: {e.service: e.minutes for e in b.estimates} for b in scenario.businesses}
            svc = {d.id: d.service for d in scenario.deliverables}
            for a in result.assignments:
                assert a.minutes == est[a.providerId][svc[a.deliverableId]]

    @_SETTINGS
    @given(valid_scenarios())
    def test_provider_supports_service(self, raw):
        """Provider must always have an estimate for the service it's assigned."""
        scenario = Scenario.model_validate(raw)
        result = optimize(scenario)
        if result.status == ResultStatus.COLLABORATION:
            est = {b.id: {e.service for e in b.estimates} for b in scenario.businesses}
            svc = {d.id: d.service for d in scenario.deliverables}
            for a in result.assignments:
                assert svc[a.deliverableId] in est[a.providerId]

    @_SETTINGS
    @given(valid_scenarios())
    def test_capacity_respected(self, raw):
        scenario = Scenario.model_validate(raw)
        result = optimize(scenario)
        if result.status == ResultStatus.COLLABORATION:
            cap = {b.id: b.capacityMinutes for b in scenario.businesses}
            for m in result.businessMetrics:
                assert m.effortMinutes <= cap[m.businessId]

    @_SETTINGS
    @given(valid_scenarios())
    def test_no_worse_off(self, raw):
        scenario = Scenario.model_validate(raw)
        result = optimize(scenario)
        if result.status == ResultStatus.COLLABORATION:
            for m in result.businessMetrics:
                assert m.netSavingsMinutes >= 0

    @_SETTINGS
    @given(valid_scenarios())
    def test_totals_sum_consistency(self, raw):
        """Totals must equal the sum of per-business metrics."""
        scenario = Scenario.model_validate(raw)
        result = optimize(scenario)
        if result.status == ResultStatus.COLLABORATION:
            m = result.businessMetrics
            t = result.totals
            assert t.baselineMinutes     == sum(x.baselineMinutes     for x in m)
            assert t.productionMinutes   == sum(x.productionMinutes   for x in m)
            assert t.coordinationMinutes == sum(x.coordinationMinutes for x in m)
            assert t.effortMinutes       == sum(x.effortMinutes       for x in m)
            assert t.netSavingsMinutes   == sum(x.netSavingsMinutes   for x in m)

    @_SETTINGS
    @given(valid_scenarios())
    def test_effort_equals_production_plus_coordination(self, raw):
        scenario = Scenario.model_validate(raw)
        result = optimize(scenario)
        if result.status == ResultStatus.COLLABORATION:
            for m in result.businessMetrics:
                assert m.effortMinutes == m.productionMinutes + m.coordinationMinutes

    @_SETTINGS
    @given(valid_scenarios())
    def test_collaboration_has_cross_and_positive_savings(self, raw):
        scenario = Scenario.model_validate(raw)
        result = optimize(scenario)
        if result.status == ResultStatus.COLLABORATION:
            owner_map = {d.id: d.ownerId for d in scenario.deliverables}
            has_cross = any(a.providerId != owner_map[a.deliverableId] for a in result.assignments)
            assert has_cross
            assert result.totals.netSavingsMinutes > 0

    @_SETTINGS
    @given(valid_scenarios())
    def test_infeasible_has_no_assignments_or_totals(self, raw):
        scenario = Scenario.model_validate(raw)
        result = optimize(scenario)
        if result.status == ResultStatus.INFEASIBLE:
            assert result.assignments == []
            assert result.totals is None
            assert result.businessMetrics is None
            assert result.certificate is None

    @_SETTINGS
    @given(valid_scenarios())
    def test_no_exchange_has_no_assignments(self, raw):
        scenario = Scenario.model_validate(raw)
        result = optimize(scenario)
        if result.status == ResultStatus.NO_EXCHANGE:
            assert result.assignments == []
            assert result.certificate is None

    @_SETTINGS
    @given(valid_scenarios())
    def test_all_minutes_are_integers(self, raw):
        scenario = Scenario.model_validate(raw)
        result = optimize(scenario)
        if result.status == ResultStatus.COLLABORATION:
            for a in result.assignments:
                assert isinstance(a.minutes, int)
            for m in result.businessMetrics:
                assert isinstance(m.effortMinutes, int)
                assert isinstance(m.netSavingsMinutes, int)


class TestOracleAgreement:

    @_SETTINGS
    @given(valid_scenarios())
    def test_status_matches_oracle(self, raw):
        """Solver status must match the independent brute-force oracle."""
        scenario = Scenario.model_validate(raw)
        solver_result = optimize(scenario)
        oracle_status, _ = _oracle_best(scenario)
        assert solver_result.status.value == oracle_status

    @_SETTINGS
    @given(valid_scenarios())
    def test_optimal_effort_matches_oracle(self, raw):
        """When both find collaboration, total effort must be identical (optimality)."""
        scenario = Scenario.model_validate(raw)
        solver_result = optimize(scenario)
        oracle_status, oracle_effort = _oracle_best(scenario)
        if solver_result.status == ResultStatus.COLLABORATION and oracle_status == "collaboration":
            assert solver_result.totals.effortMinutes == oracle_effort


class TestCertificateProperty:

    @_SETTINGS
    @given(valid_scenarios())
    def test_collaboration_certificate_verifies(self, raw):
        scenario = Scenario.model_validate(raw)
        result = optimize(scenario)
        if result.status == ResultStatus.COLLABORATION:
            vr = verify_certificate(scenario, result.assignments, result.certificate)
            assert vr.valid

    @_SETTINGS
    @given(valid_scenarios())
    def test_tampered_metrics_fail_verification(self, raw):
        """
        Tampered solver metrics must fail verify().
        We tamper by swapping a providerId to an invalid provider when possible.
        """
        scenario = Scenario.model_validate(raw)
        result = optimize(scenario)
        if result.status != ResultStatus.COLLABORATION or len(result.assignments) < 1:
            return
        # Tamper: inflate minutes on the first assignment
        tampered = list(result.assignments)
        first = tampered[0]
        bad = Assignment(
            deliverableId=first.deliverableId,
            providerId=first.providerId,
            minutes=first.minutes + 1,  # wrong minutes
        )
        tampered[0] = bad
        tampered_result = Result(
            schemaVersion="1",
            status=ResultStatus.COLLABORATION,
            assignments=tampered,
            businessMetrics=result.businessMetrics,
            totals=result.totals,
            reasons=[],
            certificate=result.certificate,
        )
        # verify() recomputes — tampered minutes won't match provider estimate → raises
        with pytest.raises((ValueError, KeyError)):
            verify(scenario, tampered_result)


class TestMonotonicFeasibility:
    """
    Increasing every capacity can only keep or expand the feasible set — it
    must never turn a feasible scenario into infeasible.  We do NOT assert that
    every individual saving increases (that is not guaranteed).
    """

    @_SETTINGS
    @given(valid_scenarios())
    def test_increasing_capacity_never_makes_infeasible_feasible_worse(self, raw):
        scenario = Scenario.model_validate(raw)
        before = optimize(scenario)

        # Raise every capacity to the maximum
        from contracts.models import CapacityChange
        changes = [
            CapacityChange(kind="capacity", businessId=b.id, minutes=1_000_000)
            for b in scenario.businesses
        ]
        patched = apply_changes(scenario, changes)
        after = optimize(patched)

        # If before was feasible (collaboration or no_exchange), after must not be infeasible.
        if before.status in (ResultStatus.COLLABORATION, ResultStatus.NO_EXCHANGE):
            assert after.status != ResultStatus.INFEASIBLE

    @_SETTINGS
    @given(valid_scenarios())
    def test_increasing_capacity_does_not_reduce_total_savings_when_collab(self, raw):
        """
        When both before and after are collaboration, raising capacity cannot
        reduce the optimal total net savings (the old optimum is still feasible).
        """
        scenario = Scenario.model_validate(raw)
        before = optimize(scenario)
        if before.status != ResultStatus.COLLABORATION:
            return

        from contracts.models import CapacityChange
        changes = [
            CapacityChange(kind="capacity", businessId=b.id, minutes=1_000_000)
            for b in scenario.businesses
        ]
        after = optimize(apply_changes(scenario, changes))
        if after.status == ResultStatus.COLLABORATION:
            assert after.totals.netSavingsMinutes >= before.totals.netSavingsMinutes


class TestDeterminism:

    @_SETTINGS
    @given(valid_scenarios())
    def test_optimize_is_deterministic(self, raw):
        scenario = Scenario.model_validate(raw)
        r1 = optimize(scenario)
        r2 = optimize(scenario)
        assert r1.status == r2.status
        pairs1 = sorted((a.deliverableId, a.providerId) for a in r1.assignments)
        pairs2 = sorted((a.deliverableId, a.providerId) for a in r2.assignments)
        assert pairs1 == pairs2
        if r1.certificate and r2.certificate:
            assert r1.certificate.allocationDigest == r2.certificate.allocationDigest
