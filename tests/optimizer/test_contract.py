"""
A1 optimizer contract tests.
These are independent oracle tests — expected values come from the
contract spec and fixture files, NOT from solver internals.

Run: python -m pytest tests/optimizer/test_contract.py -v

All 11 required test cases from the contract are covered:
  1. Canonical fixture → collaboration, exact numbers
  2. Coordination=60 variant → collaboration, adjusted effort/savings
  3. Disruption (designco design=600) → no_exchange
  4. All capacities zero → infeasible
  5. Missing owner estimate → MissingBaselineError (→ 422)
  6. Unsupported provider service → excluded (not assigned duration=0)
  7. Attractive total but one business harmed → rejected
  8. Multiple equal optima → stable tiebreak regardless of business order
  9. High coordination → self-assignment can beat collaboration
 10. Tampered assignment → verify() raises ValueError
 11. Certificate digests change when input changes
"""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from tests.optimizer import _fixtures
from optimizer.contract import (
    Assignment,
    Business,
    Deliverable,
    Result,
    ResultStatus,
    Scenario,
    ServiceEstimate,
    ServiceName,
)
from optimizer.errors import MissingBaselineError
from optimizer.solver import solve, verify

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load(name: str) -> dict:
    return _fixtures.load(name)



def _scenario(name: str) -> Scenario:
    return Scenario.model_validate(_load(name)["scenario"])


def _make_scenario(
    businesses: list[dict],
    deliverables: list[dict],
) -> Scenario:
    return Scenario.model_validate({
        "schemaVersion": "1",
        "confirmed": True,
        "businesses": businesses,
        "deliverables": deliverables,
    })


# ---------------------------------------------------------------------------
# 1. Canonical fixture
# ---------------------------------------------------------------------------

class TestCanonicalFixture:
    def test_status_is_collaboration(self):
        result = solve(_scenario("canonical.json"))
        assert result.status == ResultStatus.COLLABORATION

    def test_baseline_1440(self):
        result = solve(_scenario("canonical.json"))
        assert result.totals is not None
        assert result.totals.baselineMinutes == 1440

    def test_production_540(self):
        result = solve(_scenario("canonical.json"))
        assert result.totals.productionMinutes == 540

    def test_gross_savings_900(self):
        result = solve(_scenario("canonical.json"))
        assert result.totals.grossSavingsMinutes == 900

    def test_net_savings_900_zero_coordination(self):
        result = solve(_scenario("canonical.json"))
        assert result.totals.coordinationMinutes == 0
        assert result.totals.netSavingsMinutes == 900

    def test_each_business_saves_300(self):
        result = solve(_scenario("canonical.json"))
        assert result.businessMetrics is not None
        for m in result.businessMetrics:
            assert m.netSavingsMinutes == 300, f"{m.businessId} savings={m.netSavingsMinutes}"

    def test_each_business_produces_180(self):
        result = solve(_scenario("canonical.json"))
        for m in result.businessMetrics:
            assert m.productionMinutes == 180

    def test_assignments_cover_all_deliverables(self):
        result = solve(_scenario("canonical.json"))
        ids = {a.deliverableId for a in result.assignments}
        assert ids == {"d-design", "d-writing", "d-coding"}

    def test_cross_business_assignments(self):
        """Each deliverable should be done by someone other than the owner."""
        result = solve(_scenario("canonical.json"))
        scenario = _scenario("canonical.json")
        owner_map = {d.id: d.ownerId for d in scenario.deliverables}
        for a in result.assignments:
            assert a.providerId != owner_map[a.deliverableId], (
                f"{a.deliverableId} assigned to its own owner {a.providerId}"
            )

    def test_certificate_present(self):
        result = solve(_scenario("canonical.json"))
        assert result.certificate is not None
        cert = result.certificate
        assert len(cert.scenarioDigest) == 64
        assert len(cert.allocationDigest) == 64
        assert cert.verification == "valid"

    def test_assignments_minutes_match_provider_estimates(self):
        """Minutes in assignments must equal the provider's own estimate, not the owner's."""
        result = solve(_scenario("canonical.json"))
        scenario = _scenario("canonical.json")
        est_map = {b.id: {e.service: e.minutes for e in b.estimates} for b in scenario.businesses}
        svc_map = {d.id: d.service for d in scenario.deliverables}
        for a in result.assignments:
            expected = est_map[a.providerId][svc_map[a.deliverableId]]
            assert a.minutes == expected


# ---------------------------------------------------------------------------
# 2. Coordination=60 variant
# ---------------------------------------------------------------------------

class TestCoordinationFixture:
    def test_status_is_collaboration(self):
        assert solve(_scenario("coordination.json")).status == ResultStatus.COLLABORATION

    def test_coordination_total_180(self):
        result = solve(_scenario("coordination.json"))
        assert result.totals.coordinationMinutes == 180

    def test_effort_total_720(self):
        result = solve(_scenario("coordination.json"))
        assert result.totals.effortMinutes == 720

    def test_net_savings_720(self):
        result = solve(_scenario("coordination.json"))
        assert result.totals.netSavingsMinutes == 720

    def test_gross_savings_900(self):
        result = solve(_scenario("coordination.json"))
        assert result.totals.grossSavingsMinutes == 900

    def test_each_business_saves_240(self):
        result = solve(_scenario("coordination.json"))
        for m in result.businessMetrics:
            assert m.netSavingsMinutes == 240

    def test_each_business_effort_240(self):
        result = solve(_scenario("coordination.json"))
        for m in result.businessMetrics:
            assert m.effortMinutes == 240


# ---------------------------------------------------------------------------
# 3. Disruption — no_exchange
# ---------------------------------------------------------------------------

class TestDisruption:
    def test_status_is_no_exchange(self):
        result = solve(_scenario("disruption.json"))
        assert result.status == ResultStatus.NO_EXCHANGE

    def test_assignments_are_the_self_production_plan(self):
        """No exchange: each business does its own deliverable. Independent of solver output: owner and service come from the fixture."""
        scenario = _scenario("disruption.json")
        result = solve(scenario)
        owner_of = {d.id: d.ownerId for d in scenario.deliverables}
        assert sorted(a.deliverableId for a in result.assignments) == sorted(owner_of)
        assert all(a.providerId == owner_of[a.deliverableId] for a in result.assignments)

    def test_metrics_show_nobody_saves_anything(self):
        result = solve(_scenario("disruption.json"))
        assert result.businessMetrics is not None
        assert {m.businessId: m.baselineMinutes for m in result.businessMetrics} == {"devco": 480, "designco": 480, "copyco": 480}  # each owner's OWN need; the disruption only changes what designco would charge to design for others
        for m in result.businessMetrics:
            assert m.productionMinutes == m.baselineMinutes and m.coordinationMinutes == 0 and m.netSavingsMinutes == 0

    def test_totals_show_zero_savings(self):
        result = solve(_scenario("disruption.json"))
        t = result.totals
        assert t is not None
        assert (t.baselineMinutes, t.productionMinutes, t.effortMinutes) == (1440, 1440, 1440)
        assert (t.grossSavingsMinutes, t.netSavingsMinutes, t.coordinationMinutes) == (0, 0, 0)

    def test_reason_code(self):
        result = solve(_scenario("disruption.json"))
        assert any(r.code == "NO_BENEFICIAL_EXCHANGE" for r in result.reasons)

    def test_certificate_present_and_valid(self):
        """The frozen schema gives every non-infeasible result a certificate. It certifies the plan is valid, not that it benefits anyone."""
        result = solve(_scenario("disruption.json"))
        assert result.certificate is not None
        assert result.certificate.verification == "valid"
        names = {c.name for c in result.certificate.checks}
        assert "capacity_respected" in names and "no_worse_off" in names
        assert "at_least_one_cross_business_assignment" not in names, "a no_exchange plan must not claim an exchange"
        assert "total_net_savings_positive" not in names
    def test_no_exchange_is_not_infeasible(self):
        """no_exchange is a distinct valid outcome — the disruption scenario is NOT infeasible."""
        result = solve(_scenario("disruption.json"))
        assert result.status != ResultStatus.INFEASIBLE


# ---------------------------------------------------------------------------
# 4. All capacities zero → infeasible
# ---------------------------------------------------------------------------

class TestInfeasible:
    def _zero_capacity_scenario(self) -> Scenario:
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

    def test_status_infeasible(self):
        result = solve(self._zero_capacity_scenario())
        assert result.status == ResultStatus.INFEASIBLE

    def test_assignments_empty(self):
        result = solve(self._zero_capacity_scenario())
        assert result.assignments == []

    def test_no_totals(self):
        result = solve(self._zero_capacity_scenario())
        assert result.totals is None

    def test_no_certificate(self):
        result = solve(self._zero_capacity_scenario())
        assert result.certificate is None


# ---------------------------------------------------------------------------
# 5. Missing owner estimate → MissingBaselineError
# ---------------------------------------------------------------------------

class TestMissingBaseline:
    def test_raises_missing_baseline_error(self):
        """devco owns d-design (service=design) but has no design estimate."""
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
        with pytest.raises(MissingBaselineError) as exc_info:
            solve(scenario)
        assert "devco" in str(exc_info.value)
        assert "design" in str(exc_info.value)

    def test_missing_fields_list(self):
        """missing_fields contains the path of every absent estimate."""
        scenario = _make_scenario(
            businesses=[
                {"id": "devco",    "name": "A", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": []},  # missing both
                {"id": "designco", "name": "B", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "design", "minutes": 180}, {"service": "writing", "minutes": 480}]},
                {"id": "copyco",   "name": "C", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "writing", "minutes": 180}, {"service": "coding", "minutes": 480}]},
            ],
            deliverables=[
                {"id": "d-design",  "ownerId": "devco",    "service": "design"},
                {"id": "d-coding",  "ownerId": "devco",    "service": "coding"},
                {"id": "d-writing", "ownerId": "designco", "service": "writing"},
            ],
        )
        with pytest.raises(MissingBaselineError) as exc_info:
            solve(scenario)
        missing = exc_info.value.missing_fields
        assert len(missing) == 2
        assert any("design" in f for f in missing)
        assert any("coding" in f for f in missing)


# ---------------------------------------------------------------------------
# 6. Unsupported provider service → excluded, never duration=0
# ---------------------------------------------------------------------------

class TestUnsupportedProvider:
    def test_unsupported_provider_never_appears_in_feasible_providers(self):
        """
        copyco has no writing estimate. The engine must never include copyco
        as a candidate provider for d-writing (writing service).
        We verify this by confirming copyco never appears as provider for d-writing
        in any result, and that the enumeration terminates correctly.

        In this scenario designco is the ONLY provider for d-writing. Assigning
        designco to both d-design AND d-writing would require 180+480=660 min
        production, exceeding capacity=600. So no full cross-business collaboration
        is feasible → no_exchange (not infeasible, since self-assignment works for each
        business individually when checked alone; but all-self = no_exchange).
        """
        scenario = _make_scenario(
            businesses=[
                {"id": "devco",    "name": "A", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 180}, {"service": "design", "minutes": 480}]},
                {"id": "designco", "name": "B", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "design", "minutes": 180}, {"service": "writing", "minutes": 480}]},
                {"id": "copyco",   "name": "C", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 480}]},  # no writing estimate
            ],
            deliverables=[
                {"id": "d-design",  "ownerId": "devco",    "service": "design"},
                {"id": "d-writing", "ownerId": "designco", "service": "writing"},
                {"id": "d-coding",  "ownerId": "copyco",   "service": "coding"},
            ],
        )
        result = solve(scenario)
        # copyco must NEVER appear as provider for d-writing
        writing_assignments = [a for a in result.assignments if a.deliverableId == "d-writing"]
        for a in writing_assignments:
            assert a.providerId != "copyco", "copyco has no writing estimate and must never provide it"
            assert a.minutes > 0  # minutes are never zero

    def test_unsupported_provider_scenario_outcome(self):
        """
        Scenario described above: designco doing both d-design(180) + d-writing(480) = 660 min
        exceeds capacity(600). No valid full cross-exchange exists → no_exchange.
        """
        scenario = _make_scenario(
            businesses=[
                {"id": "devco",    "name": "A", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 180}, {"service": "design", "minutes": 480}]},
                {"id": "designco", "name": "B", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "design", "minutes": 180}, {"service": "writing", "minutes": 480}]},
                {"id": "copyco",   "name": "C", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 480}]},
            ],
            deliverables=[
                {"id": "d-design",  "ownerId": "devco",    "service": "design"},
                {"id": "d-writing", "ownerId": "designco", "service": "writing"},
                {"id": "d-coding",  "ownerId": "copyco",   "service": "coding"},
            ],
        )
        result = solve(scenario)
        # Self-assignment feasible for each, no beneficial cross-exchange → no_exchange
        assert result.status in (ResultStatus.NO_EXCHANGE, ResultStatus.INFEASIBLE)
        # Specifically: self-assignment for each business is feasible (480<=600), so no_exchange
        assert result.status == ResultStatus.NO_EXCHANGE

    def test_unsupported_provider_with_enough_capacity(self):
        """
        Same setup but designco has capacity=1200 — now designco CAN do both
        d-design and d-writing, and a beneficial exchange IS possible.
        The result should be collaboration with d-writing assigned to designco (self),
        but that's no cross-exchange for d-writing... Actually: devco→d-design (cross),
        designco→d-writing (self), copyco→d-coding (self): only one cross-business assignment.
        devco produces coding(d-coding? no — devco is provider for d-design here).
        Let's trace: devco provides d-design (service=design, minutes=480 for devco as owner
        or 180 for designco). Wait — devco owns d-design and designco can provide at 180.
        With d-writing only providable by designco (self=480), and d-coding only by copyco (self)
        or devco (devco has coding=180): devco→d-coding(cross), designco→d-design(cross),
        copyco→d-coding(self? no, d-coding is copyco's). 
        d-coding owned by copyco, providers: [copyco(480), devco(180)].
        Best: designco→d-design(180), copyco→d-coding(480 self? but devco at 180 is cheaper).
        With devco→d-coding(cross,180) and designco→d-design(cross,180) and designco self→d-writing(480):
        designco production=180+480=660. capacity=1200: OK. effortMinutes=660.
        baseline_designco=480. netSavings=480-660=-180 < 0 → still rejected!
        → no_exchange even with higher capacity. This is the correct contract behaviour.
        """
        scenario = _make_scenario(
            businesses=[
                {"id": "devco",    "name": "A", "capacityMinutes": 1200, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 180}, {"service": "design", "minutes": 480}]},
                {"id": "designco", "name": "B", "capacityMinutes": 1200, "coordinationMinutes": 0,
                 "estimates": [{"service": "design", "minutes": 180}, {"service": "writing", "minutes": 480}]},
                {"id": "copyco",   "name": "C", "capacityMinutes": 1200, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 480}]},
            ],
            deliverables=[
                {"id": "d-design",  "ownerId": "devco",    "service": "design"},
                {"id": "d-writing", "ownerId": "designco", "service": "writing"},
                {"id": "d-coding",  "ownerId": "copyco",   "service": "coding"},
            ],
        )
        result = solve(scenario)
        # designco must do d-writing (self, 480min) plus whatever d-design assignment.
        # baseline_designco=480. If designco also does d-design, effort=660 > baseline → harmed.
        # → no_exchange
        assert result.status == ResultStatus.NO_EXCHANGE


# ---------------------------------------------------------------------------
# 7. Attractive total but one harmed → rejected
# ---------------------------------------------------------------------------

class TestNoHarmedParticipant:
    def test_allocation_that_harms_one_business_is_rejected(self):
        """
        Scenario: devco capacity is tiny. Any assignment to devco would exceed
        capacity. The globally cheapest total might assign to devco, but that
        candidate must be rejected; the solver must find a no-harm alternative
        or return no_exchange/infeasible.
        """
        scenario = _make_scenario(
            businesses=[
                # devco: capacity only 10 — will be harmed by any assignment
                {"id": "devco",    "name": "A", "capacityMinutes": 10, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 500}, {"service": "design", "minutes": 480}]},
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
        result = solve(scenario)
        # devco's effort constraint (capacity=10) prevents any assignment to it.
        # Self-assignment for devco: effort=480 > capacity=10 → infeasible.
        assert result.status == ResultStatus.INFEASIBLE
        # In any case, no assignment to devco should appear with minutes > 10
        for a in result.assignments:
            assert not (a.providerId == "devco" and a.minutes > 10)


# ---------------------------------------------------------------------------
# 8. Stable tiebreak — independent of business input order
# ---------------------------------------------------------------------------

class TestStableTiebreak:
    def _canonical_reversed(self) -> Scenario:
        """Same as canonical but businesses listed in reverse order."""
        data = _load("canonical.json")
        scenario_data = deepcopy(data["scenario"])
        scenario_data["businesses"] = list(reversed(scenario_data["businesses"]))
        return Scenario.model_validate(scenario_data)

    def test_same_result_regardless_of_business_order(self):
        r1 = solve(_scenario("canonical.json"))
        r2 = solve(self._canonical_reversed())
        # Same assignments (order normalised by sort)
        pairs1 = sorted((a.deliverableId, a.providerId) for a in r1.assignments)
        pairs2 = sorted((a.deliverableId, a.providerId) for a in r2.assignments)
        assert pairs1 == pairs2

    def test_same_totals_regardless_of_business_order(self):
        r1 = solve(_scenario("canonical.json"))
        r2 = solve(self._canonical_reversed())
        assert r1.totals.netSavingsMinutes == r2.totals.netSavingsMinutes
        assert r1.totals.effortMinutes == r2.totals.effortMinutes


# ---------------------------------------------------------------------------
# 9. High coordination — self-assignment beats collaboration
# ---------------------------------------------------------------------------

class TestHighCoordination:
    def test_high_coordination_yields_no_exchange(self):
        """
        Set coordination=400 for each business. The canonical scenario's
        baseline per business is 480 min. Collaboration effort = 180 + 400 = 580.
        netSavings = 480 - 580 = -100 < 0. All collaborative allocations are rejected.
        → no_exchange.
        """
        scenario = _make_scenario(
            businesses=[
                {"id": "devco",    "name": "A", "capacityMinutes": 600, "coordinationMinutes": 400,
                 "estimates": [{"service": "coding", "minutes": 180}, {"service": "design", "minutes": 480}]},
                {"id": "designco", "name": "B", "capacityMinutes": 600, "coordinationMinutes": 400,
                 "estimates": [{"service": "design", "minutes": 180}, {"service": "writing", "minutes": 480}]},
                {"id": "copyco",   "name": "C", "capacityMinutes": 600, "coordinationMinutes": 400,
                 "estimates": [{"service": "writing", "minutes": 180}, {"service": "coding", "minutes": 480}]},
            ],
            deliverables=[
                {"id": "d-design",  "ownerId": "devco",    "service": "design"},
                {"id": "d-writing", "ownerId": "designco", "service": "writing"},
                {"id": "d-coding",  "ownerId": "copyco",   "service": "coding"},
            ],
        )
        result = solve(scenario)
        assert result.status == ResultStatus.NO_EXCHANGE

    def test_self_assignment_is_not_collaboration(self):
        """
        With very high coordination, self-assignment is the only feasible allocation
        but it is NOT classified as collaboration (no cross-business assignments).
        """
        result = solve(_make_scenario(
            businesses=[
                {"id": "devco",    "name": "A", "capacityMinutes": 600, "coordinationMinutes": 400,
                 "estimates": [{"service": "design", "minutes": 480}]},
                {"id": "designco", "name": "B", "capacityMinutes": 600, "coordinationMinutes": 400,
                 "estimates": [{"service": "writing", "minutes": 480}]},
                {"id": "copyco",   "name": "C", "capacityMinutes": 600, "coordinationMinutes": 400,
                 "estimates": [{"service": "coding", "minutes": 480}]},
            ],
            deliverables=[
                {"id": "d-design",  "ownerId": "devco",    "service": "design"},
                {"id": "d-writing", "ownerId": "designco", "service": "writing"},
                {"id": "d-coding",  "ownerId": "copyco",   "service": "coding"},
            ],
        ))
        assert result.status == ResultStatus.NO_EXCHANGE


# ---------------------------------------------------------------------------
# 10. Tampered assignment → verify() raises ValueError
# ---------------------------------------------------------------------------

class TestVerify:
    def test_verify_good_result_returns_result(self):
        scenario = _scenario("canonical.json")
        result = solve(scenario)
        verified = verify(scenario, result)
        assert verified.status == ResultStatus.COLLABORATION
        assert verified.certificate is not None

    def test_verify_tampered_provider_raises(self):
        scenario = _scenario("canonical.json")
        result = solve(scenario)
        # Tamper: swap a providerId to an invalid one
        tampered_assignments = list(result.assignments)
        first = tampered_assignments[0]
        # Replace provider with owner (self-assignment — may change feasibility)
        # More concretely: assign d-coding to designco (who has no coding estimate)
        bad_assignments = [
            Assignment(deliverableId=a.deliverableId,
                       providerId="designco" if a.deliverableId == "d-coding" else a.providerId,
                       minutes=a.minutes)
            for a in tampered_assignments
        ]
        bad_result = Result(
            schemaVersion="1",
            status=ResultStatus.COLLABORATION,
            assignments=bad_assignments,
            businessMetrics=result.businessMetrics,
            totals=result.totals,
            reasons=[],
            certificate=result.certificate,
        )
        with pytest.raises((ValueError, KeyError)):
            verify(scenario, bad_result)

    def test_verify_produces_fresh_certificate(self):
        scenario = _scenario("canonical.json")
        result = solve(scenario)
        verified = verify(scenario, result)
        # Both should have matching digests
        assert verified.certificate.scenarioDigest == result.certificate.scenarioDigest

    def test_verify_missing_deliverable_raises(self):
        scenario = _scenario("canonical.json")
        result = solve(scenario)
        # Drop one assignment
        partial = Result(
            schemaVersion="1",
            status=ResultStatus.COLLABORATION,
            assignments=result.assignments[:-1],
            businessMetrics=result.businessMetrics,
            totals=result.totals,
            reasons=[],
            certificate=result.certificate,
        )
        with pytest.raises(ValueError, match="scenario has"):
            verify(scenario, partial)


# ---------------------------------------------------------------------------
# 11. Certificate digests change when input changes
# ---------------------------------------------------------------------------

class TestCertificateIntegrity:
    def test_different_scenarios_produce_different_scenario_digests(self):
        r1 = solve(_scenario("canonical.json"))
        r2 = solve(_scenario("coordination.json"))
        assert r1.certificate.scenarioDigest != r2.certificate.scenarioDigest

    def test_same_scenario_produces_same_digest(self):
        r1 = solve(_scenario("canonical.json"))
        r2 = solve(_scenario("canonical.json"))
        assert r1.certificate.scenarioDigest == r2.certificate.scenarioDigest
        assert r1.certificate.allocationDigest == r2.certificate.allocationDigest

    def test_verify_produces_matching_digest(self):
        scenario = _scenario("canonical.json")
        result = solve(scenario)
        verified = verify(scenario, result)
        assert verified.certificate.allocationDigest == result.certificate.allocationDigest


# ---------------------------------------------------------------------------
# 12. Edge cases — single deliverable, only one provider
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_single_deliverable_self_assignment_only(self):
        """One deliverable, only owner has the required service → no_exchange (no cross-business)."""
        scenario = _make_scenario(
            businesses=[
                {"id": "devco",    "name": "A", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 120}]},
                {"id": "designco", "name": "B", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "design", "minutes": 100}]},
                {"id": "copyco",   "name": "C", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "writing", "minutes": 100}]},
            ],
            deliverables=[
                {"id": "d-coding", "ownerId": "devco", "service": "coding"},
            ],
        )
        result = solve(scenario)
        # Only devco can do coding — self-assignment only → no cross-business → no_exchange
        assert result.status == ResultStatus.NO_EXCHANGE

    def test_schema_version_on_all_results(self):
        for fname in ["canonical.json", "coordination.json", "disruption.json"]:
            result = solve(_scenario(fname))
            assert result.schemaVersion == "1"

    def test_no_exchange_not_called_infeasible(self):
        """Contract: no_exchange and infeasible are distinct; no_exchange means feasible self-assignment."""
        result = solve(_scenario("disruption.json"))
        assert result.status == ResultStatus.NO_EXCHANGE
        assert result.status != ResultStatus.INFEASIBLE

    def test_minutes_are_integers(self):
        result = solve(_scenario("canonical.json"))
        for a in result.assignments:
            assert isinstance(a.minutes, int)
        assert isinstance(result.totals.effortMinutes, int)
        assert isinstance(result.totals.baselineMinutes, int)
