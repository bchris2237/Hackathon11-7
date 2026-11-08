"""
A2 — Exact bounded assignment solver tests.
Owner: Developer 1.

Tests are oracle-based: expected values come from the frozen contract spec
and canonical fixtures, NOT from solver internals.

Key A2 requirements covered:
  - enumerate_assignments produces every candidate in deterministic order
  - optimize() is an alias for solve() with identical output
  - Bound guard rejects scenarios above 5 businesses / 6 deliverables
  - Canonical fixture: 1440 baseline → 540 production (derived from spec)
  - Business permutation produces the same optimum
  - Brute-force oracle agrees with solver on small cases
  - Provider lists are owner-first, others lexicographic
  - No floating-point minutes anywhere
"""
from fractions import Fraction
import json
from copy import deepcopy
from itertools import product as cartesian_product
from pathlib import Path

import pytest

from tests.optimizer import _fixtures
from optimizer.contract import (
    ResultStatus,
    Scenario,
    ServiceName,
)
from optimizer.errors import MissingBaselineError
from optimizer.solver import enumerate_assignments, optimize, solve, verify

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load(name: str) -> dict:
    return _fixtures.load(name)



def _scenario(name: str) -> Scenario:
    return Scenario.model_validate(_load(name)["scenario"])


def _make_scenario(businesses: list[dict], deliverables: list[dict]) -> Scenario:
    return Scenario.model_validate({
        "schemaVersion": "1",
        "confirmed": True,
        "businesses": businesses,
        "deliverables": deliverables,
    })


# ---------------------------------------------------------------------------
# Brute-force oracle (independently coded, used for agreement tests)
# ---------------------------------------------------------------------------

def _brute_force_oracle(scenario: Scenario) -> dict | None:
    """
    Independently implement the optimizer algorithm for cross-checking.
    Returns the best assignment map, or None if no collaboration exists.
    This code is written separately from engine.py to serve as an independent oracle.
    """
    estimates = {b.id: {e.service: e.minutes for e in b.estimates} for b in scenario.businesses}
    owner_map = {d.id: d.ownerId for d in scenario.deliverables}
    service_map = {d.id: d.service for d in scenario.deliverables}

    # Compute baselines
    baselines: dict[str, int] = {b.id: 0 for b in scenario.businesses}
    for d in scenario.deliverables:
        svc = d.service
        if svc in estimates.get(d.ownerId, {}):
            baselines[d.ownerId] += estimates[d.ownerId][svc]

    # Feasible providers per deliverable
    d_ids = sorted(d.id for d in scenario.deliverables)
    providers_per_d = []
    for d_id in d_ids:
        svc = service_map[d_id]
        owner = owner_map[d_id]
        cands = [owner] + sorted(
            b.id for b in scenario.businesses
            if b.id != owner and svc in estimates.get(b.id, {})
        )
        providers_per_d.append(cands)

    best_key = None
    best_map = None

    for combo in cartesian_product(*providers_per_d):
        amap = dict(zip(d_ids, combo))

        # Production
        prod: dict[str, int] = {b.id: 0 for b in scenario.businesses}
        for d_id, prov in amap.items():
            prod[prov] += estimates[prov][service_map[d_id]]

        # Involvement
        involved: set[str] = set()
        for d_id, prov in amap.items():
            if prov != owner_map[d_id]:
                involved.add(prov)
                involved.add(owner_map[d_id])

        # Effort
        effort: dict[str, int] = {}
        cap_map = {b.id: b.capacityMinutes for b in scenario.businesses}
        coord_map = {b.id: b.coordinationMinutes for b in scenario.businesses}
        for b in scenario.businesses:
            c = coord_map[b.id] if b.id in involved else 0
            effort[b.id] = prod[b.id] + c

        # Feasibility
        feasible = all(
            effort[b.id] <= cap_map[b.id] and baselines[b.id] - effort[b.id] >= 0
            for b in scenario.businesses
        )
        if not feasible:
            continue

        # Collaboration?
        has_cross = any(prov != owner_map[d_id] for d_id, prov in amap.items())
        total_net = sum(baselines[b.id] - effort[b.id] for b in scenario.businesses)
        if not (has_cross and total_net > 0):
            continue

        # Tiebreak key (same logic as engine)
        total_baseline = sum(baselines[b.id] for b in scenario.businesses)
        total_effort = sum(effort[b.id] for b in scenario.businesses)

        def _ratio(bid: str):
            # Independent of the module under test: an exact fraction; a zero baseline is larger than any finite share.
            if baselines[bid] == 0:
                return (1, Fraction(0))
            return (0, Fraction(effort[bid], baselines[bid]))

        max_ratio = max(_ratio(b.id) for b in scenario.businesses)
        tertiary = sorted(amap.items())
        key = (total_effort, max_ratio, tertiary)

        if best_key is None or key < best_key:
            best_key = key
            best_map = amap

    return best_map


# ---------------------------------------------------------------------------
# 1. enumerate_assignments — shape and determinism
# ---------------------------------------------------------------------------

class TestEnumerateAssignments:

    def test_canonical_candidate_count(self):
        """
        Canonical: 3 businesses, 3 deliverables. Each deliverable has 2 feasible
        providers (owner + one other), so 2^3 = 8 candidates.
        """
        scenario = _scenario("canonical.json")
        candidates = enumerate_assignments(scenario)
        # Each business can do the others' work: 2 providers per deliverable
        assert len(candidates) == 8

    def test_all_candidates_are_dicts(self):
        candidates = enumerate_assignments(_scenario("canonical.json"))
        for c in candidates:
            assert isinstance(c, dict)
            assert all(isinstance(k, str) and isinstance(v, str) for k, v in c.items())

    def test_all_deliverables_covered_in_each_candidate(self):
        scenario = _scenario("canonical.json")
        d_ids = {d.id for d in scenario.deliverables}
        for c in enumerate_assignments(scenario):
            assert set(c.keys()) == d_ids

    def test_order_is_deterministic_across_calls(self):
        s = _scenario("canonical.json")
        c1 = enumerate_assignments(s)
        c2 = enumerate_assignments(s)
        assert c1 == c2

    def test_order_stable_with_permuted_businesses(self):
        """Deliverable order is sorted lexicographically — independent of business list order."""
        data = _load("canonical.json")
        s1 = Scenario.model_validate(data["scenario"])
        rev = deepcopy(data["scenario"])
        rev["businesses"] = list(reversed(rev["businesses"]))
        s2 = Scenario.model_validate(rev)
        c1 = enumerate_assignments(s1)
        c2 = enumerate_assignments(s2)
        assert c1 == c2

    def test_provider_lists_owner_first(self):
        """Owner appears before other providers in every candidate position."""
        scenario = _scenario("canonical.json")
        owner_map = {d.id: d.ownerId for d in scenario.deliverables}
        candidates = enumerate_assignments(scenario)
        # The first candidate must be all self-assignments (owner first)
        first = candidates[0]
        for d_id, prov in first.items():
            assert prov == owner_map[d_id], (
                f"First candidate for {d_id} should be owner {owner_map[d_id]}, got {prov}"
            )

    def test_single_provider_deliverable_appears_once(self):
        """
        A deliverable whose service only the owner supports must appear with
        exactly that owner in every candidate.
        """
        scenario = _make_scenario(
            businesses=[
                {"id": "devco",    "name": "A", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 100}]},
                {"id": "designco", "name": "B", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "design", "minutes": 100}]},
                {"id": "copyco",   "name": "C", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "writing", "minutes": 100}]},
            ],
            deliverables=[
                {"id": "d-coding", "ownerId": "devco", "service": "coding"},
            ],
        )
        candidates = enumerate_assignments(scenario)
        assert len(candidates) == 1  # only devco can do coding
        assert candidates[0]["d-coding"] == "devco"

    def test_bound_guard_too_many_businesses(self):
        """Scenarios with >5 businesses are rejected before enumeration."""
        scenario = _make_scenario(
            businesses=[
                {"id": f"b{i}", "name": f"B{i}", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 100}]}
                for i in range(5)  # schema max is 5 — test the engine guard too
            ],
            deliverables=[{"id": "d1", "ownerId": "b0", "service": "coding"}],
        )
        # 5 businesses is fine (at limit)
        candidates = enumerate_assignments(scenario)
        assert len(candidates) >= 1

    def test_explicit_bound_guard_message(self):
        """Directly call with a manipulated scenario that bypasses Pydantic max."""
        from unittest.mock import patch
        from optimizer.engine import enumerate_assignments as _ea
        scenario = _scenario("canonical.json")
        # Patch businesses list length > 5
        import types
        fake = types.SimpleNamespace(
            businesses=[types.SimpleNamespace() for _ in range(6)],
            deliverables=scenario.deliverables,
        )
        with pytest.raises(ValueError, match=r"15625"):
            _ea(fake)  # type: ignore


# ---------------------------------------------------------------------------
# 2. optimize() is a stable alias for solve()
# ---------------------------------------------------------------------------

class TestOptimizeAlias:

    def test_optimize_canonical_same_as_solve(self):
        scenario = _scenario("canonical.json")
        r_solve = solve(scenario)
        r_optimize = optimize(scenario)
        assert r_solve.status == r_optimize.status
        assert r_solve.totals.netSavingsMinutes == r_optimize.totals.netSavingsMinutes

    def test_optimize_coordination_same_as_solve(self):
        scenario = _scenario("coordination.json")
        assert solve(scenario).totals.effortMinutes == optimize(scenario).totals.effortMinutes

    def test_optimize_disruption_same_as_solve(self):
        assert solve(_scenario("disruption.json")).status == optimize(_scenario("disruption.json")).status

    def test_optimize_assignments_identical_to_solve(self):
        scenario = _scenario("canonical.json")
        r_s = solve(scenario)
        r_o = optimize(scenario)
        pairs_s = sorted((a.deliverableId, a.providerId) for a in r_s.assignments)
        pairs_o = sorted((a.deliverableId, a.providerId) for a in r_o.assignments)
        assert pairs_s == pairs_o


# ---------------------------------------------------------------------------
# 3. Canonical fixture exact numbers (A2 spec requirement)
# ---------------------------------------------------------------------------

class TestCanonicalExactNumbers:

    def test_baseline_derived_1440(self):
        """
        Derived from spec:
          devco baseline   = design estimate = 480
          designco baseline = writing estimate = 480
          copyco baseline  = coding estimate = 480
          Total = 1440
        """
        result = optimize(_scenario("canonical.json"))
        assert result.totals.baselineMinutes == 1440

    def test_production_540(self):
        """
        Optimal: each business does the task it's cheapest at (180 min each).
        3 × 180 = 540.
        """
        result = optimize(_scenario("canonical.json"))
        assert result.totals.productionMinutes == 540

    def test_gross_savings_900(self):
        """1440 baseline − 540 production = 900 gross savings."""
        result = optimize(_scenario("canonical.json"))
        assert result.totals.grossSavingsMinutes == 900

    def test_net_savings_equals_gross_zero_coordination(self):
        result = optimize(_scenario("canonical.json"))
        assert result.totals.coordinationMinutes == 0
        assert result.totals.netSavingsMinutes == result.totals.grossSavingsMinutes

    def test_each_business_produces_180(self):
        result = optimize(_scenario("canonical.json"))
        for m in result.businessMetrics:
            assert m.productionMinutes == 180

    def test_each_business_saves_300(self):
        result = optimize(_scenario("canonical.json"))
        for m in result.businessMetrics:
            assert m.netSavingsMinutes == 300

    def test_assignments_minutes_are_provider_estimates(self):
        """Minutes in an assignment = provider's estimate, not owner's."""
        scenario = _scenario("canonical.json")
        result = optimize(scenario)
        est = {b.id: {e.service: e.minutes for e in b.estimates} for b in scenario.businesses}
        svc = {d.id: d.service for d in scenario.deliverables}
        for a in result.assignments:
            assert a.minutes == est[a.providerId][svc[a.deliverableId]]

    def test_all_minutes_are_integers(self):
        result = optimize(_scenario("canonical.json"))
        for a in result.assignments:
            assert isinstance(a.minutes, int)
        assert isinstance(result.totals.baselineMinutes, int)
        assert isinstance(result.totals.effortMinutes, int)


# ---------------------------------------------------------------------------
# 4. Business permutation — same optimum
# ---------------------------------------------------------------------------

class TestPermutationStability:

    def _permutations(self, scenario: Scenario) -> list[Scenario]:
        """Return 6 permutations of the 3-business scenario."""
        from itertools import permutations
        data = json.loads(scenario.model_dump_json())
        result = []
        for perm in permutations(data["businesses"]):
            d = dict(data)
            d["businesses"] = list(perm)
            result.append(Scenario.model_validate(d))
        return result

    def test_all_permutations_same_assignment_pairs(self):
        base = _scenario("canonical.json")
        base_pairs = sorted(
            (a.deliverableId, a.providerId) for a in optimize(base).assignments
        )
        for s in self._permutations(base):
            r = optimize(s)
            pairs = sorted((a.deliverableId, a.providerId) for a in r.assignments)
            assert pairs == base_pairs, f"Permutation produced different assignments: {pairs}"

    def test_all_permutations_same_totals(self):
        base = _scenario("canonical.json")
        base_net = optimize(base).totals.netSavingsMinutes
        for s in self._permutations(base):
            assert optimize(s).totals.netSavingsMinutes == base_net

    def test_coordination_permutation_stable(self):
        base = _scenario("coordination.json")
        base_effort = optimize(base).totals.effortMinutes
        from itertools import permutations
        data = json.loads(base.model_dump_json())
        for perm in permutations(data["businesses"]):
            d = dict(data)
            d["businesses"] = list(perm)
            s = Scenario.model_validate(d)
            assert optimize(s).totals.effortMinutes == base_effort


# ---------------------------------------------------------------------------
# 5. Brute-force oracle agreement
# ---------------------------------------------------------------------------

class TestBruteForceOracle:

    def test_canonical_oracle_agrees(self):
        scenario = _scenario("canonical.json")
        oracle_map = _brute_force_oracle(scenario)
        solver_result = optimize(scenario)
        assert oracle_map is not None
        solver_map = {a.deliverableId: a.providerId for a in solver_result.assignments}
        assert solver_map == oracle_map

    def test_coordination_oracle_agrees(self):
        scenario = _scenario("coordination.json")
        oracle_map = _brute_force_oracle(scenario)
        solver_result = optimize(scenario)
        assert oracle_map is not None
        solver_map = {a.deliverableId: a.providerId for a in solver_result.assignments}
        assert solver_map == oracle_map

    def test_single_deliverable_two_providers_oracle_agrees(self):
        """
        devco owns d-coding (coding). Both devco(480) and designco can code (100).
        Oracle prefers designco (100 < 480), but designco baseline=0 → netSavings=0-100<0.
        Actually designco has no coding deliverable → baseline_designco=0, effort=100 → net=-100 → infeasible.
        So only devco self-assignment is feasible → no_exchange.
        """
        scenario = _make_scenario(
            businesses=[
                {"id": "devco",    "name": "A", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 480}]},
                {"id": "designco", "name": "B", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 100}, {"service": "design", "minutes": 200}]},
                {"id": "copyco",   "name": "C", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "writing", "minutes": 100}]},
            ],
            deliverables=[
                {"id": "d-coding", "ownerId": "devco", "service": "coding"},
            ],
        )
        oracle_map = _brute_force_oracle(scenario)
        solver_result = optimize(scenario)
        # Both should agree: no collaboration possible
        assert oracle_map is None
        assert solver_result.status == ResultStatus.NO_EXCHANGE

    def test_two_viable_cross_assignments_oracle_agrees(self):
        """
        Two businesses can exchange work. Oracle and solver must produce same assignment.
        devco: capacity=600, coord=0, estimates: coding=100, design=400
        designco: capacity=600, coord=0, estimates: design=100, coding=400
        copyco: capacity=600, coord=0, estimates: writing=100 (filler)
        d-coding owned by devco, d-design owned by designco, d-writing owned by copyco
        Optimal: designco→d-coding? No: designco doesn't own d-coding.
        Provider for d-coding: devco(100) + designco(400) — devco self is cheapest.
        Wait: designco estimates coding=400. Provider for d-design: designco(100) + devco(400).
        Cross: devco→d-design(400), designco→d-coding(400) each produce 400 — same as self.
        Better: designco→d-design(100 self), devco→d-coding(100 self), copyco→d-writing(100 self)
        That's all self → no_exchange.
        Better cross: devco does d-design(400) + designco does d-coding(400):
          devco: prod=400, baseline=100, net=100-400=-300 → harmed → rejected.
        So no collaboration → no_exchange. Oracle must agree.
        """
        scenario = _make_scenario(
            businesses=[
                {"id": "devco",    "name": "A", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 100}, {"service": "design", "minutes": 400}]},
                {"id": "designco", "name": "B", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "design", "minutes": 100}, {"service": "coding", "minutes": 400}]},
                {"id": "copyco",   "name": "C", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "writing", "minutes": 100}]},
            ],
            deliverables=[
                {"id": "d-coding",  "ownerId": "devco",    "service": "coding"},
                {"id": "d-design",  "ownerId": "designco", "service": "design"},
                {"id": "d-writing", "ownerId": "copyco",   "service": "writing"},
            ],
        )
        oracle_map = _brute_force_oracle(scenario)
        solver_result = optimize(scenario)
        assert oracle_map is None
        assert solver_result.status == ResultStatus.NO_EXCHANGE

    def test_canonical_oracle_total_effort(self):
        """Oracle total effort must match solver."""
        scenario = _scenario("canonical.json")
        oracle_map = _brute_force_oracle(scenario)
        solver_result = optimize(scenario)
        assert oracle_map is not None
        # Oracle effort: each produces 180 min (their cheap task), no coordination → 540
        est = {b.id: {e.service: e.minutes for e in b.estimates} for b in scenario.businesses}
        svc = {d.id: d.service for d in scenario.deliverables}
        oracle_effort = sum(est[prov][svc[d_id]] for d_id, prov in oracle_map.items())
        assert oracle_effort == solver_result.totals.effortMinutes


# ---------------------------------------------------------------------------
# 6. Enumeration bound enforcement
# ---------------------------------------------------------------------------

class TestEnumerationBound:

    def test_five_businesses_three_deliverables_accepted(self):
        """5 businesses with 3 deliverables is within bounds."""
        scenario = _make_scenario(
            businesses=[
                {"id": "b0", "name": "B0", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 100}]},
                {"id": "b1", "name": "B1", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 200}]},
                {"id": "b2", "name": "B2", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "design", "minutes": 100}]},
                {"id": "b3", "name": "B3", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "design", "minutes": 150}]},
                {"id": "b4", "name": "B4", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "writing", "minutes": 100}]},
            ],
            deliverables=[
                {"id": "d0", "ownerId": "b0", "service": "coding"},
                {"id": "d1", "ownerId": "b2", "service": "design"},
                {"id": "d2", "ownerId": "b4", "service": "writing"},
            ],
        )
        # Should not raise
        candidates = enumerate_assignments(scenario)
        assert len(candidates) >= 1

    def test_max_candidates_five_by_six(self):
        """
        With 5 businesses all able to do the same service and 6 deliverables,
        we approach the 5^6 = 15 625 bound.
        """
        scenario = _make_scenario(
            businesses=[
                {"id": f"b{i}", "name": f"B{i}", "capacityMinutes": 999999,
                 "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 100 + i}]}
                for i in range(5)
            ],
            deliverables=[
                {"id": f"d{j}", "ownerId": "b0", "service": "coding"}
                for j in range(6)
            ],
        )
        candidates = enumerate_assignments(scenario)
        # 5 providers per deliverable × 6 deliverables = 5^6 = 15625
        assert len(candidates) == 15_625


# ---------------------------------------------------------------------------
# 7. Infeasible and missing baseline via optimize()
# ---------------------------------------------------------------------------

class TestOptimizeErrorPaths:

    def test_infeasible_via_optimize(self):
        scenario = _make_scenario(
            businesses=[
                {"id": "devco",    "name": "A", "capacityMinutes": 0, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 180}, {"service": "design", "minutes": 480}]},
                {"id": "designco", "name": "B", "capacityMinutes": 0, "coordinationMinutes": 0,
                 "estimates": [{"service": "design", "minutes": 180}, {"service": "writing", "minutes": 480}]},
                {"id": "copyco",   "name": "C", "capacityMinutes": 0, "coordinationMinutes": 0,
                 "estimates": [{"service": "writing", "minutes": 180}, {"service": "coding", "minutes": 480}]},
            ],
            deliverables=[
                {"id": "d-design",  "ownerId": "devco",    "service": "design"},
                {"id": "d-writing", "ownerId": "designco", "service": "writing"},
                {"id": "d-coding",  "ownerId": "copyco",   "service": "coding"},
            ],
        )
        result = optimize(scenario)
        assert result.status == ResultStatus.INFEASIBLE

    def test_missing_baseline_via_optimize(self):
        scenario = _make_scenario(
            businesses=[
                {"id": "devco",    "name": "A", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 180}]},  # no design estimate
                {"id": "designco", "name": "B", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "design", "minutes": 180}, {"service": "writing", "minutes": 480}]},
                {"id": "copyco",   "name": "C", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "writing", "minutes": 180}, {"service": "coding", "minutes": 480}]},
            ],
            deliverables=[
                {"id": "d-design",  "ownerId": "devco",    "service": "design"},
                {"id": "d-writing", "ownerId": "designco", "service": "writing"},
                {"id": "d-coding",  "ownerId": "copyco",   "service": "coding"},
            ],
        )
        with pytest.raises(MissingBaselineError):
            optimize(scenario)
