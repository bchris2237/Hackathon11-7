"""
A6 — Deterministic what-if engine tests.
Owner: Developer 1.

Tests apply_changes and diff_results from optimizer/simulation.py.

Required A6 acceptance cases:
  - designco design=600 → no_exchange (not infeasible or forced loop)
  - Zero all capacities → infeasible
  - Original scenario preserved after apply_changes
  - Invalid patch fails atomically (no partial application)
  - Repeated calculation is deterministic
  - Duplicate target rejected
  - Unknown businessId rejected
  - Estimate added when service not previously present
  - diff_results correct deltas
  - status_changed flag set correctly
"""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from contracts.models import Scenario
from optimizer.simulation import (
    ResultDiff,
    SimulationError,
    apply_changes,
    diff_results,
)
from optimizer.solver import optimize

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


def _scenario(name: str) -> Scenario:
    return Scenario.model_validate(_load(name)["scenario"])


def _make_scenario(businesses: list[dict], deliverables: list[dict]) -> Scenario:
    return Scenario.model_validate({
        "schemaVersion": "1",
        "confirmed": True,
        "businesses": businesses,
        "deliverables": deliverables,
    })


def _capacity_change(business_id: str, minutes: int) -> dict:
    return {"kind": "capacity", "businessId": business_id, "minutes": minutes}


def _coordination_change(business_id: str, minutes: int) -> dict:
    return {"kind": "coordination", "businessId": business_id, "minutes": minutes}


def _estimate_change(business_id: str, service: str, minutes: int) -> dict:
    return {"kind": "estimate", "businessId": business_id, "service": service, "minutes": minutes}


def _parse_changes(raw: list[dict]) -> list:
    """Parse raw change dicts into typed Change objects via Pydantic."""
    from contracts.models import CapacityChange, CoordinationChange, EstimateChange
    result = []
    for c in raw:
        if c["kind"] == "capacity":
            result.append(CapacityChange(**c))
        elif c["kind"] == "coordination":
            result.append(CoordinationChange(**c))
        else:
            result.append(EstimateChange(**c))
    return result


# ===========================================================================
# apply_changes — happy path
# ===========================================================================

class TestApplyChangesHappyPath:

    def test_original_preserved_capacity_change(self):
        """A6 required: original scenario must be unchanged after apply_changes."""
        scenario = _scenario("canonical.json")
        original_cap = next(b.capacityMinutes for b in scenario.businesses if b.id == "devco")
        changes = _parse_changes([_capacity_change("devco", 999)])
        patched = apply_changes(scenario, changes)
        # Original unchanged
        assert next(b.capacityMinutes for b in scenario.businesses if b.id == "devco") == original_cap
        # Patched updated
        assert next(b.capacityMinutes for b in patched.businesses if b.id == "devco") == 999

    def test_original_preserved_coordination_change(self):
        scenario = _scenario("canonical.json")
        changes = _parse_changes([_coordination_change("devco", 100)])
        patched = apply_changes(scenario, changes)
        assert next(b.coordinationMinutes for b in scenario.businesses if b.id == "devco") == 0
        assert next(b.coordinationMinutes for b in patched.businesses if b.id == "devco") == 100

    def test_original_preserved_estimate_change(self):
        scenario = _scenario("canonical.json")
        changes = _parse_changes([_estimate_change("designco", "design", 600)])
        patched = apply_changes(scenario, changes)
        devco_est_orig = {e.service.value: e.minutes for e in next(
            b for b in scenario.businesses if b.id == "designco").estimates}
        assert devco_est_orig["design"] == 180  # original unchanged
        devco_est_pat = {e.service.value: e.minutes for e in next(
            b for b in patched.businesses if b.id == "designco").estimates}
        assert devco_est_pat["design"] == 600   # patched updated

    def test_capacity_change_applied(self):
        scenario = _scenario("canonical.json")
        changes = _parse_changes([_capacity_change("copyco", 1200)])
        patched = apply_changes(scenario, changes)
        assert next(b.capacityMinutes for b in patched.businesses if b.id == "copyco") == 1200

    def test_coordination_change_applied(self):
        scenario = _scenario("canonical.json")
        changes = _parse_changes([_coordination_change("copyco", 60)])
        patched = apply_changes(scenario, changes)
        assert next(b.coordinationMinutes for b in patched.businesses if b.id == "copyco") == 60

    def test_estimate_update_existing_service(self):
        scenario = _scenario("canonical.json")
        changes = _parse_changes([_estimate_change("designco", "design", 600)])
        patched = apply_changes(scenario, changes)
        ests = {e.service.value: e.minutes for e in
                next(b for b in patched.businesses if b.id == "designco").estimates}
        assert ests["design"] == 600

    def test_estimate_adds_new_service(self):
        """When a business has no estimate for a service, apply_changes adds it."""
        scenario = _scenario("canonical.json")
        # devco has no writing estimate
        changes = _parse_changes([_estimate_change("devco", "writing", 300)])
        patched = apply_changes(scenario, changes)
        ests = {e.service.value: e.minutes for e in
                next(b for b in patched.businesses if b.id == "devco").estimates}
        assert ests["writing"] == 300

    def test_multiple_changes_applied(self):
        scenario = _scenario("canonical.json")
        changes = _parse_changes([
            _capacity_change("devco", 1000),
            _coordination_change("designco", 50),
            _estimate_change("copyco", "design", 200),
        ])
        patched = apply_changes(scenario, changes)
        assert next(b.capacityMinutes      for b in patched.businesses if b.id == "devco")    == 1000
        assert next(b.coordinationMinutes  for b in patched.businesses if b.id == "designco") == 50
        copyco_ests = {e.service.value: e.minutes for e in
                       next(b for b in patched.businesses if b.id == "copyco").estimates}
        assert copyco_ests.get("design") == 200

    def test_empty_changes_returns_copy(self):
        scenario = _scenario("canonical.json")
        patched = apply_changes(scenario, [])
        assert patched is not scenario
        assert patched.model_dump_json() == scenario.model_dump_json()

    def test_zero_capacity_accepted(self):
        """Zero is a valid capacity value (will make scenario infeasible, but is a valid patch)."""
        scenario = _scenario("canonical.json")
        changes = _parse_changes([_capacity_change("devco", 0)])
        patched = apply_changes(scenario, changes)
        assert next(b.capacityMinutes for b in patched.businesses if b.id == "devco") == 0

    def test_zero_coordination_accepted(self):
        scenario = _scenario("coordination.json")
        changes = _parse_changes([_coordination_change("devco", 0)])
        patched = apply_changes(scenario, changes)
        assert next(b.coordinationMinutes for b in patched.businesses if b.id == "devco") == 0

    def test_patched_scenario_passes_pydantic(self):
        """Result of apply_changes must be a valid Pydantic Scenario."""
        scenario = _scenario("canonical.json")
        changes = _parse_changes([_capacity_change("devco", 300)])
        patched = apply_changes(scenario, changes)
        assert isinstance(patched, Scenario)
        assert patched.confirmed is True
        assert patched.schemaVersion == "1"


# ===========================================================================
# apply_changes — validation failures (atomic rejection)
# ===========================================================================

class TestApplyChangesValidation:

    def test_unknown_business_id_rejected(self):
        scenario = _scenario("canonical.json")
        changes = _parse_changes([_capacity_change("ghost-co", 500)])
        with pytest.raises(SimulationError, match="unknown businessId"):
            apply_changes(scenario, changes)

    def test_original_unchanged_after_rejection(self):
        """A6 required: patch fails atomically — original must be untouched."""
        scenario = _scenario("canonical.json")
        orig_json = scenario.model_dump_json()
        # Mix valid and invalid changes
        changes = _parse_changes([
            _capacity_change("devco", 1000),   # valid
            _capacity_change("ghost", 500),    # invalid
        ])
        with pytest.raises(SimulationError):
            apply_changes(scenario, changes)
        # Original unchanged
        assert scenario.model_dump_json() == orig_json

    def test_duplicate_capacity_target_rejected(self):
        """A6 required: same (kind, businessId) twice → rejected atomically."""
        scenario = _scenario("canonical.json")
        changes = _parse_changes([
            _capacity_change("devco", 100),
            _capacity_change("devco", 200),  # duplicate
        ])
        with pytest.raises(SimulationError, match="duplicate"):
            apply_changes(scenario, changes)

    def test_duplicate_coordination_target_rejected(self):
        scenario = _scenario("canonical.json")
        changes = _parse_changes([
            _coordination_change("designco", 50),
            _coordination_change("designco", 60),  # duplicate
        ])
        with pytest.raises(SimulationError, match="duplicate"):
            apply_changes(scenario, changes)

    def test_duplicate_estimate_target_rejected(self):
        scenario = _scenario("canonical.json")
        changes = _parse_changes([
            _estimate_change("designco", "design", 200),
            _estimate_change("designco", "design", 300),  # duplicate
        ])
        with pytest.raises(SimulationError, match="duplicate"):
            apply_changes(scenario, changes)

    def test_same_business_different_services_allowed(self):
        """Different services for same business are NOT duplicates."""
        scenario = _scenario("canonical.json")
        changes = _parse_changes([
            _estimate_change("devco", "coding", 200),
            _estimate_change("devco", "design", 400),  # same business, different service → ok
        ])
        patched = apply_changes(scenario, changes)  # should not raise
        ests = {e.service.value: e.minutes for e in
                next(b for b in patched.businesses if b.id == "devco").estimates}
        assert ests["coding"] == 200
        assert ests["design"] == 400


# ===========================================================================
# Integration: apply_changes + optimize
# ===========================================================================

class TestApplyChangesIntegration:

    def test_designco_design_600_yields_no_exchange(self):
        """
        A6 required: designco design=600 → no_exchange (not infeasible).
        This matches the disruption fixture exactly.
        """
        scenario = _scenario("canonical.json")
        changes = _parse_changes([_estimate_change("designco", "design", 600)])
        patched = apply_changes(scenario, changes)
        result = optimize(patched)
        assert result.status.value == "no_exchange", (
            f"Expected no_exchange but got {result.status.value}"
        )

    def test_zero_all_capacities_yields_infeasible(self):
        """A6 required: all capacities zero → infeasible."""
        scenario = _scenario("canonical.json")
        changes = _parse_changes([
            _capacity_change("devco",    0),
            _capacity_change("designco", 0),
            _capacity_change("copyco",   0),
        ])
        patched = apply_changes(scenario, changes)
        result = optimize(patched)
        assert result.status.value == "infeasible"

    def test_repeated_calculation_is_deterministic(self):
        """A6 required: same changes applied twice must produce identical results."""
        scenario = _scenario("canonical.json")
        changes = _parse_changes([
            _coordination_change("devco",    60),
            _coordination_change("designco", 60),
            _coordination_change("copyco",   60),
        ])
        patched1 = apply_changes(scenario, changes)
        patched2 = apply_changes(scenario, changes)
        result1 = optimize(patched1)
        result2 = optimize(patched2)
        assert result1.status == result2.status
        assert result1.totals.netSavingsMinutes == result2.totals.netSavingsMinutes
        pairs1 = sorted((a.deliverableId, a.providerId) for a in result1.assignments)
        pairs2 = sorted((a.deliverableId, a.providerId) for a in result2.assignments)
        assert pairs1 == pairs2

    def test_coordination_60_result_matches_fixture(self):
        """
        Applying coordination=60 to canonical should produce the coordination fixture result.
        """
        scenario = _scenario("canonical.json")
        changes = _parse_changes([
            _coordination_change("devco",    60),
            _coordination_change("designco", 60),
            _coordination_change("copyco",   60),
        ])
        patched = apply_changes(scenario, changes)
        result = optimize(patched)
        assert result.status.value == "collaboration"
        assert result.totals.coordinationMinutes == 180
        assert result.totals.effortMinutes == 720
        assert result.totals.netSavingsMinutes == 720

    def test_increasing_capacity_makes_infeasible_feasible(self):
        """
        Start with zero capacity (infeasible), then set capacity to 600 → collaboration.
        """
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
        before = optimize(scenario)
        assert before.status.value == "infeasible"

        changes = _parse_changes([
            _capacity_change("devco",    600),
            _capacity_change("designco", 600),
            _capacity_change("copyco",   600),
        ])
        patched = apply_changes(scenario, changes)
        after = optimize(patched)
        assert after.status.value == "collaboration"

    def test_original_not_reoptimized_after_patch(self):
        """Patching should not change the original scenario's optimization result."""
        scenario = _scenario("canonical.json")
        before = optimize(scenario)

        changes = _parse_changes([_estimate_change("designco", "design", 600)])
        patched = apply_changes(scenario, changes)
        optimize(patched)  # run on patched

        # Original result must be reproducible
        after_original = optimize(scenario)
        assert before.status == after_original.status
        assert before.totals.netSavingsMinutes == after_original.totals.netSavingsMinutes


# ===========================================================================
# diff_results
# ===========================================================================

class TestDiffResults:

    def _before(self):
        return optimize(_scenario("canonical.json"))

    def test_same_result_zero_deltas(self):
        result = self._before()
        diff = diff_results(result, result)
        assert diff.total_effort_delta == 0
        assert diff.total_net_savings_delta == 0
        assert not diff.status_changed

    def test_status_changed_flag(self):
        scenario = _scenario("canonical.json")
        before = optimize(scenario)

        changes = _parse_changes([_estimate_change("designco", "design", 600)])
        patched = apply_changes(scenario, changes)
        after = optimize(patched)

        diff = diff_results(before, after)
        assert diff.status_changed
        assert diff.before_status == "collaboration"
        assert diff.after_status  == "no_exchange"

    def test_effort_delta_with_coordination(self):
        """Adding coordination increases effort → positive effort_delta."""
        scenario = _scenario("canonical.json")
        before = optimize(scenario)

        changes = _parse_changes([
            _coordination_change("devco",    60),
            _coordination_change("designco", 60),
            _coordination_change("copyco",   60),
        ])
        patched = apply_changes(scenario, changes)
        after = optimize(patched)

        diff = diff_results(before, after)
        assert diff.total_effort_delta == 180   # 720 - 540
        assert diff.total_net_savings_delta == -180  # 720 - 900

    def test_improved_flag_after_fix(self):
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
        before = optimize(scenario)
        changes = _parse_changes([
            _capacity_change("devco",    600),
            _capacity_change("designco", 600),
            _capacity_change("copyco",   600),
        ])
        after = optimize(apply_changes(scenario, changes))
        diff = diff_results(before, after)
        assert diff.improved
        assert not diff.degraded

    def test_degraded_flag_when_infeasible_after(self):
        scenario = _scenario("canonical.json")
        before = optimize(scenario)
        changes = _parse_changes([
            _capacity_change("devco",    0),
            _capacity_change("designco", 0),
            _capacity_change("copyco",   0),
        ])
        after = optimize(apply_changes(scenario, changes))
        diff = diff_results(before, after)
        assert diff.degraded
        assert not diff.improved

    def test_business_deltas_present(self):
        scenario = _scenario("canonical.json")
        before = optimize(scenario)
        changes = _parse_changes([
            _coordination_change("devco",    60),
            _coordination_change("designco", 60),
            _coordination_change("copyco",   60),
        ])
        after = optimize(apply_changes(scenario, changes))
        diff = diff_results(before, after)
        assert len(diff.business_deltas) == 3
        for bd in diff.business_deltas:
            assert bd.effortDelta == 60          # coordination added
            assert bd.netSavingsDelta == -60     # savings reduced by coordination

    def test_infeasible_to_infeasible_no_deltas(self):
        scenario = _make_scenario(
            businesses=[
                {"id": "devco",    "name": "A", "capacityMinutes": 0, "coordinationMinutes": 0,
                 "estimates": [{"service": "design", "minutes": 480}]},
                {"id": "designco", "name": "B", "capacityMinutes": 0, "coordinationMinutes": 0,
                 "estimates": [{"service": "writing", "minutes": 480}]},
                {"id": "copyco",   "name": "C", "capacityMinutes": 0, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 480}]},
            ],
            deliverables=[
                {"id": "d-design",  "ownerId": "devco",    "service": "design"},
                {"id": "d-writing", "ownerId": "designco", "service": "writing"},
                {"id": "d-coding",  "ownerId": "copyco",   "service": "coding"},
            ],
        )
        before = optimize(scenario)
        after  = optimize(scenario)
        diff = diff_results(before, after)
        assert diff.before_status == "infeasible"
        assert diff.after_status  == "infeasible"
        assert not diff.status_changed
        # Both totals are None → deltas are None
        assert diff.total_effort_delta is None
        assert diff.total_net_savings_delta is None

    def test_result_diff_type(self):
        result = self._before()
        diff = diff_results(result, result)
        assert isinstance(diff, ResultDiff)
