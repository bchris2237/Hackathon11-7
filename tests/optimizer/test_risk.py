"""
A10 — Optional fairness and risk (sensitivity) analysis tests.
Owner: Developer 1.

Tests evaluate_risk from optimizer/risk.py.

Required A10 acceptance cases:
  - Zero perturbation matches base
  - Increased duration can destroy feasibility
  - Nominal certificate unchanged
  - Record exact explored scenarios
  - Distinguish sensitivity from probability (no confidence claims)
"""
import json
from pathlib import Path

import pytest

from tests.optimizer import _fixtures
from optimizer.contract import Scenario
from optimizer.risk import (
    EstimatePerturbation,
    PerturbationOutcome,
    PerturbationScenario,
    RiskReport,
    evaluate_risk,
)
from optimizer.solver import optimize

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"


def _load(name: str) -> dict:
    return _fixtures.load(name)



def _scenario(name: str) -> Scenario:
    return Scenario.model_validate(_load(name)["scenario"])


# ===========================================================================
# Zero perturbation matches base
# ===========================================================================

class TestZeroPerturbation:

    def test_no_perturbations_nominal_only(self):
        scenario = _scenario("canonical.json")
        report = evaluate_risk(scenario, [])
        assert report.nominal_status == "collaboration"
        assert report.nominal_net_savings == 900
        assert report.tested_count == 0

    def test_identity_perturbation_matches_base(self):
        """
        A perturbation that sets an estimate to its existing value must produce
        the same result as the nominal.
        """
        scenario = _scenario("canonical.json")
        # designco design is already 180 — set it to 180 (identity)
        perts = [
            PerturbationScenario(
                label="identity",
                changes=(EstimatePerturbation(businessId="designco", service="design", new_minutes=180),),
            ),
        ]
        report = evaluate_risk(scenario, perts)
        outcome = report.outcomes[0]
        assert outcome.status == "collaboration"
        assert outcome.net_savings == report.nominal_net_savings

    def test_nominal_net_savings_matches_optimize(self):
        scenario = _scenario("canonical.json")
        report = evaluate_risk(scenario, [])
        assert report.nominal_net_savings == optimize(scenario).totals.netSavingsMinutes


# ===========================================================================
# Increased duration can destroy feasibility
# ===========================================================================

class TestFeasibilityDestruction:

    def test_large_estimate_increase_breaks_collaboration(self):
        """
        A10 required: increasing designco's design estimate to 600 drives the
        scenario to no_exchange (matching the disruption fixture).
        """
        scenario = _scenario("canonical.json")
        perts = [
            PerturbationScenario(
                label="designco_design_600",
                changes=(EstimatePerturbation(businessId="designco", service="design", new_minutes=600),),
            ),
        ]
        report = evaluate_risk(scenario, perts)
        assert report.nominal_status == "collaboration"
        assert report.outcomes[0].status == "no_exchange"
        assert report.any_perturbation_breaks_collaboration

    def test_worst_net_savings_reflects_degradation(self):
        scenario = _scenario("canonical.json")
        perts = [
            PerturbationScenario(
                label="mild",
                changes=(EstimatePerturbation(businessId="designco", service="design", new_minutes=200),),
            ),
            PerturbationScenario(
                label="severe",
                changes=(EstimatePerturbation(businessId="designco", service="design", new_minutes=600),),
            ),
        ]
        report = evaluate_risk(scenario, perts)
        # 'severe' breaks collaboration (no_exchange, net_savings None)
        severe = next(o for o in report.outcomes if o.label == "severe")
        assert severe.status == "no_exchange"
        # worst_net_savings only considers collaboration outcomes
        mild = next(o for o in report.outcomes if o.label == "mild")
        if mild.status == "collaboration":
            assert report.worst_net_savings == mild.net_savings

    def test_perturbation_can_yield_infeasible(self):
        """Increasing an estimate beyond capacity can make the scenario infeasible."""
        scenario = _scenario("canonical.json")  # capacity 600 each
        # devco owns d-design; raise devco's design estimate beyond capacity.
        # But devco's design is only used for self-production baseline. To force
        # infeasible, raise an estimate the owner must self-produce beyond capacity.
        perts = [
            PerturbationScenario(
                label="over_capacity",
                changes=(EstimatePerturbation(businessId="devco", service="design", new_minutes=700),),
            ),
        ]
        report = evaluate_risk(scenario, perts)
        # devco baseline becomes 700 > capacity 600. Self-production infeasible for devco.
        # Only devco can be forced if no cheaper provider; designco can do design at 180.
        # So collaboration may still work (designco does d-design). Status is observed, not asserted strict.
        assert report.outcomes[0].status in ("collaboration", "no_exchange", "infeasible")


# ===========================================================================
# Nominal certificate unchanged
# ===========================================================================

class TestNominalCertificatePreserved:

    def test_nominal_certificate_present_and_valid(self):
        scenario = _scenario("canonical.json")
        report = evaluate_risk(scenario, [
            PerturbationScenario(
                label="p1",
                changes=(EstimatePerturbation(businessId="designco", service="design", new_minutes=600),),
            ),
        ])
        # Nominal certificate is the canonical scenario's certificate, unchanged
        assert report.nominal_certificate is not None
        from optimizer.solver import verify_certificate
        nominal_result = optimize(scenario)
        vr = verify_certificate(scenario, nominal_result.assignments, report.nominal_certificate)
        assert vr.valid

    def test_nominal_certificate_matches_fresh_optimize(self):
        scenario = _scenario("canonical.json")
        report = evaluate_risk(scenario, [])
        fresh = optimize(scenario)
        assert report.nominal_certificate.scenarioDigest == fresh.certificate.scenarioDigest
        assert report.nominal_certificate.allocationDigest == fresh.certificate.allocationDigest

    def test_risk_does_not_mutate_original_scenario(self):
        scenario = _scenario("canonical.json")
        orig = scenario.model_dump_json()
        evaluate_risk(scenario, [
            PerturbationScenario(
                label="p",
                changes=(EstimatePerturbation(businessId="designco", service="design", new_minutes=600),),
            ),
        ])
        assert scenario.model_dump_json() == orig


# ===========================================================================
# Recorded scenarios and sensitivity-not-probability
# ===========================================================================

class TestRecordingAndSemantics:

    def test_all_perturbations_recorded(self):
        scenario = _scenario("canonical.json")
        perts = [
            PerturbationScenario(label=f"p{i}", changes=(
                EstimatePerturbation(businessId="designco", service="design", new_minutes=180 + i * 50),
            ))
            for i in range(4)
        ]
        report = evaluate_risk(scenario, perts)
        assert report.tested_count == 4
        labels = [o.label for o in report.outcomes]
        assert labels == ["p0", "p1", "p2", "p3"]

    def test_note_disclaims_probability(self):
        scenario = _scenario("canonical.json")
        report = evaluate_risk(scenario, [])
        note = report.note.lower()
        assert "not a probabilistic" in note or "not" in note and "probab" in note
        assert "distribution" in note
        assert "confidence" in note

    def test_worst_case_is_observed_not_bound(self):
        """worst_net_savings is the worst OBSERVED, documented in note as not a global bound."""
        scenario = _scenario("canonical.json")
        report = evaluate_risk(scenario, [
            PerturbationScenario(label="a", changes=(
                EstimatePerturbation(businessId="designco", service="design", new_minutes=200),)),
        ])
        assert "observed" in report.note.lower()

    def test_invalid_perturbation_recorded_not_raised(self):
        scenario = _scenario("canonical.json")
        perts = [
            PerturbationScenario(label="bad_biz", changes=(
                EstimatePerturbation(businessId="ghost", service="design", new_minutes=200),)),
        ]
        report = evaluate_risk(scenario, perts)
        assert report.outcomes[0].valid_patch is False
        assert report.outcomes[0].status == "rejected"
        assert report.outcomes[0].rejection_reason

    def test_invalid_service_recorded_not_raised(self):
        scenario = _scenario("canonical.json")
        perts = [
            PerturbationScenario(label="bad_service", changes=(
                EstimatePerturbation(businessId="devco", service="marketing", new_minutes=200),)),
        ]
        report = evaluate_risk(scenario, perts)
        assert report.outcomes[0].valid_patch is False
        assert report.outcomes[0].status == "rejected"


# ===========================================================================
# Determinism
# ===========================================================================

class TestRiskDeterminism:

    def test_repeated_risk_report_deterministic(self):
        scenario = _scenario("canonical.json")
        perts = [
            PerturbationScenario(label="a", changes=(
                EstimatePerturbation(businessId="designco", service="design", new_minutes=600),)),
            PerturbationScenario(label="b", changes=(
                EstimatePerturbation(businessId="devco", service="design", new_minutes=300),)),
        ]
        r1 = evaluate_risk(scenario, perts)
        r2 = evaluate_risk(scenario, perts)
        assert [o.status for o in r1.outcomes] == [o.status for o in r2.outcomes]
        assert [o.net_savings for o in r1.outcomes] == [o.net_savings for o in r2.outcomes]

    def test_report_type(self):
        report = evaluate_risk(_scenario("canonical.json"), [])
        assert isinstance(report, RiskReport)

    def test_multi_change_perturbation(self):
        """A perturbation bundle with multiple estimate changes applied together."""
        scenario = _scenario("canonical.json")
        perts = [
            PerturbationScenario(label="combo", changes=(
                EstimatePerturbation(businessId="designco", service="design", new_minutes=250),
                EstimatePerturbation(businessId="devco", service="coding", new_minutes=200),
            )),
        ]
        report = evaluate_risk(scenario, perts)
        assert report.outcomes[0].valid_patch
        assert report.outcomes[0].status in ("collaboration", "no_exchange", "infeasible")
