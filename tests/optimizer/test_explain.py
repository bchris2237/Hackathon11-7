"""
A8 — Grounded explanations and sensitivity tests.
Owner: Developer 1.

Tests analyze_bottlenecks and sensitivity_report from optimizer/explain.py.
Every claim is backed by an actual solver rerun — no estimated or minimal-repair claims.

Required A8 acceptance cases:
  - Capacity bottleneck where a specified increase repairs it (verified by rerun)
  - A case where no tested repair works
  - Never promise global minimum repair (report states scope explicitly)
  - Zero/baseline analysis grounded in computed violations
"""
import json
from pathlib import Path

import pytest

from tests.optimizer import _fixtures
from optimizer.contract import CapacityChange, EstimateChange, Scenario
from optimizer.explain import (
    BottleneckReport,
    SensitivityReport,
    TestedCandidate,
    analyze_bottlenecks,
    sensitivity_report,
)
from optimizer.solver import optimize

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"


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


def _zero_cap_scenario() -> Scenario:
    return _make_scenario(
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


# ===========================================================================
# analyze_bottlenecks
# ===========================================================================

class TestAnalyzeBottlenecks:

    def test_healthy_scenario_no_bottlenecks(self):
        report = analyze_bottlenecks(_scenario("canonical.json"))
        assert report.status == "collaboration"
        assert not report.has_bottlenecks

    def test_zero_capacity_reports_binding_constraints(self):
        report = analyze_bottlenecks(_zero_cap_scenario())
        assert report.status == "infeasible"
        assert report.has_bottlenecks
        # Each business's self-production (480) exceeds capacity 0
        assert len(report.binding) == 3
        for bc in report.binding:
            assert bc.kind == "capacity"
            assert bc.excessMinutes == 480

    def test_partial_capacity_binding(self):
        scenario = _make_scenario(
            businesses=[
                {"id": "devco",    "name": "A", "capacityMinutes": 100, "coordinationMinutes": 0,
                 "estimates": [{"service": "design", "minutes": 480}, {"service": "coding", "minutes": 180}]},
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
        report = analyze_bottlenecks(scenario)
        # devco: self-production 480 > capacity 100 → binding
        devco_binding = [bc for bc in report.binding if bc.businessId == "devco"]
        assert len(devco_binding) == 1
        assert devco_binding[0].excessMinutes == 380

    def test_missing_baseline_reported(self):
        scenario = _make_scenario(
            businesses=[
                {"id": "devco",    "name": "A", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 180}]},  # no design
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
        report = analyze_bottlenecks(scenario)
        assert report.missing_baselines
        assert any("devco" in m and "design" in m for m in report.missing_baselines)

    def test_report_note_disclaims_root_cause(self):
        report = analyze_bottlenecks(_zero_cap_scenario())
        assert "not a proof" in report.note.lower() or "root cause" in report.note.lower()

    def test_report_type(self):
        assert isinstance(analyze_bottlenecks(_scenario("canonical.json")), BottleneckReport)


# ===========================================================================
# sensitivity_report — repair found
# ===========================================================================

class TestSensitivityRepairFound:

    def test_capacity_increase_repairs_infeasible(self):
        """
        A8 required: a specified capacity increase repairs an infeasible scenario,
        VERIFIED by an actual rerun.
        """
        scenario = _zero_cap_scenario()
        candidates = [
            ("raise_all_to_600", [
                CapacityChange(kind="capacity", businessId="devco",    minutes=600),
                CapacityChange(kind="capacity", businessId="designco", minutes=600),
                CapacityChange(kind="capacity", businessId="copyco",   minutes=600),
            ]),
        ]
        report = sensitivity_report(scenario, candidates)
        assert report.baseline_status == "infeasible"
        assert report.candidate_bound == 1
        assert report.any_repair_found
        repaired = report.repairs()
        assert len(repaired) == 1
        assert repaired[0].label == "raise_all_to_600"
        assert repaired[0].status == "collaboration"

    def test_repair_actually_works_via_optimize(self):
        """The recommended change must actually produce collaboration when applied."""
        scenario = _zero_cap_scenario()
        candidates = [
            ("fix", [
                CapacityChange(kind="capacity", businessId="devco",    minutes=600),
                CapacityChange(kind="capacity", businessId="designco", minutes=600),
                CapacityChange(kind="capacity", businessId="copyco",   minutes=600),
            ]),
        ]
        report = sensitivity_report(scenario, candidates)
        tested = report.tested[0]
        assert tested.repaired
        # Independently confirm by applying and optimizing
        from optimizer.solver import apply_changes
        patched = apply_changes(scenario, candidates[0][1])
        assert optimize(patched).status.value == "collaboration"

    def test_repaired_reports_net_savings_delta(self):
        scenario = _zero_cap_scenario()
        candidates = [
            ("fix", [
                CapacityChange(kind="capacity", businessId="devco",    minutes=600),
                CapacityChange(kind="capacity", businessId="designco", minutes=600),
                CapacityChange(kind="capacity", businessId="copyco",   minutes=600),
            ]),
        ]
        report = sensitivity_report(scenario, candidates)
        tested = report.tested[0]
        # baseline was infeasible, after is collaboration with 900 net savings
        assert tested.net_savings_delta == 900


# ===========================================================================
# sensitivity_report — no repair works
# ===========================================================================

class TestSensitivityNoRepair:

    def test_no_tested_repair_works(self):
        """A8 required: a case where no tested repair works."""
        scenario = _zero_cap_scenario()
        # Only raise one business's capacity — still infeasible (others at 0)
        candidates = [
            ("raise_devco_only", [
                CapacityChange(kind="capacity", businessId="devco", minutes=600),
            ]),
            ("raise_designco_only", [
                CapacityChange(kind="capacity", businessId="designco", minutes=600),
            ]),
        ]
        report = sensitivity_report(scenario, candidates)
        assert not report.any_repair_found
        assert report.repairs() == []
        # Both tested but neither repaired
        assert len(report.tested) == 2
        for t in report.tested:
            assert not t.repaired
            assert t.status == "infeasible"

    def test_report_note_disclaims_global_minimum(self):
        """A8 required: never promise global minimum repair."""
        scenario = _zero_cap_scenario()
        report = sensitivity_report(scenario, [])
        assert "minimum repair" in report.note.lower()
        assert "untested" in report.note.lower() or "only the explicitly" in report.note.lower()

    def test_empty_candidates_no_repair(self):
        scenario = _zero_cap_scenario()
        report = sensitivity_report(scenario, [])
        assert report.candidate_bound == 0
        assert not report.any_repair_found


# ===========================================================================
# sensitivity_report — invalid patch handling
# ===========================================================================

class TestSensitivityInvalidPatch:

    def test_invalid_patch_recorded_not_raised(self):
        """An invalid change (unknown business) is recorded as rejected, not raised."""
        scenario = _scenario("canonical.json")
        candidates = [
            ("bad_business", [
                CapacityChange(kind="capacity", businessId="ghost-co", minutes=600),
            ]),
        ]
        report = sensitivity_report(scenario, candidates)
        tested = report.tested[0]
        assert not tested.valid_patch
        assert tested.status == "rejected"
        assert not tested.repaired
        assert "unknown businessId" in tested.rejection_reason

    def test_mixed_valid_invalid_candidates(self):
        scenario = _zero_cap_scenario()
        candidates = [
            ("invalid", [CapacityChange(kind="capacity", businessId="nobody", minutes=600)]),
            ("valid_fix", [
                CapacityChange(kind="capacity", businessId="devco",    minutes=600),
                CapacityChange(kind="capacity", businessId="designco", minutes=600),
                CapacityChange(kind="capacity", businessId="copyco",   minutes=600),
            ]),
        ]
        report = sensitivity_report(scenario, candidates)
        assert len(report.tested) == 2
        assert not report.tested[0].valid_patch
        assert report.tested[1].repaired


# ===========================================================================
# sensitivity_report — disruption repair via estimate change
# ===========================================================================

class TestSensitivityDisruption:

    def test_lowering_designco_estimate_repairs_no_exchange(self):
        """
        Disruption: designco design=600 → no_exchange.
        Lowering it back to 180 should repair to collaboration (verified by rerun).
        """
        scenario = _scenario("disruption.json")
        assert optimize(scenario).status.value == "no_exchange"

        candidates = [
            ("restore_designco_design", [
                EstimateChange(kind="estimate", businessId="designco", service="design", minutes=180),
            ]),
        ]
        report = sensitivity_report(scenario, candidates)
        assert report.baseline_status == "no_exchange"
        assert report.any_repair_found
        assert report.tested[0].status == "collaboration"

    def test_candidate_bound_recorded(self):
        scenario = _scenario("disruption.json")
        candidates = [
            ("a", [EstimateChange(kind="estimate", businessId="designco", service="design", minutes=180)]),
            ("b", [EstimateChange(kind="estimate", businessId="designco", service="design", minutes=200)]),
            ("c", [EstimateChange(kind="estimate", businessId="designco", service="design", minutes=300)]),
        ]
        report = sensitivity_report(scenario, candidates)
        assert report.candidate_bound == 3
        assert len(report.tested) == 3


# ===========================================================================
# Determinism and non-mutation
# ===========================================================================

class TestSensitivityDeterminismAndSafety:

    def test_original_scenario_not_mutated(self):
        scenario = _zero_cap_scenario()
        orig_json = scenario.model_dump_json()
        sensitivity_report(scenario, [
            ("fix", [CapacityChange(kind="capacity", businessId="devco", minutes=600)]),
        ])
        assert scenario.model_dump_json() == orig_json

    def test_repeated_report_deterministic(self):
        scenario = _zero_cap_scenario()
        candidates = [
            ("fix", [
                CapacityChange(kind="capacity", businessId="devco",    minutes=600),
                CapacityChange(kind="capacity", businessId="designco", minutes=600),
                CapacityChange(kind="capacity", businessId="copyco",   minutes=600),
            ]),
        ]
        r1 = sensitivity_report(scenario, candidates)
        r2 = sensitivity_report(scenario, candidates)
        assert r1.tested[0].status == r2.tested[0].status
        assert r1.tested[0].net_savings_delta == r2.tested[0].net_savings_delta

    def test_report_type(self):
        report = sensitivity_report(_scenario("canonical.json"), [])
        assert isinstance(report, SensitivityReport)
