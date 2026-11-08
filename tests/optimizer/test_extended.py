"""
A9 — Bounded four- and five-company support tests (Optional).
Owner: Developer 1.

Tests the extended-mode gate in optimizer/models.py and the config-threaded
solve/optimize. Compares 4/5-business solutions to an independent oracle,
confirms the MVP default rejects >3 businesses, and confirms the contract
rejects 6 businesses / 7 deliverables at parse time.

Runtime observations are recorded WITHOUT performance guarantees.
"""
import json
import time
from itertools import product as cartesian_product
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.optimizer import _fixtures
from optimizer.contract import Scenario
from optimizer.models import (
    DEFAULT_CONFIG,
    MAX_CANDIDATES,
    ExtendedModeError,
    OptimizerConfig,
    candidate_count_upper_bound,
    validate_scenario_bounds,
)
from optimizer.solver import optimize

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"

EXTENDED = OptimizerConfig(extended_mode=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_scenario(businesses: list[dict], deliverables: list[dict]) -> Scenario:
    return Scenario.model_validate({
        "schemaVersion": "1",
        "confirmed": True,
        "businesses": businesses,
        "deliverables": deliverables,
    })


def _four_business_scenario() -> Scenario:
    """
    4 businesses, 4 deliverables. Each business is strong at one service
    and weak at others, so cross-business exchange is beneficial.
    """
    return _make_scenario(
        businesses=[
            {"id": "devco",    "name": "Dev",    "capacityMinutes": 600, "coordinationMinutes": 0,
             "estimates": [{"service": "coding", "minutes": 120}, {"service": "design", "minutes": 400}]},
            {"id": "designco", "name": "Design", "capacityMinutes": 600, "coordinationMinutes": 0,
             "estimates": [{"service": "design", "minutes": 120}, {"service": "writing", "minutes": 400}]},
            {"id": "copyco",   "name": "Copy",   "capacityMinutes": 600, "coordinationMinutes": 0,
             "estimates": [{"service": "writing", "minutes": 120}, {"service": "coding", "minutes": 400}]},
            {"id": "hybridco", "name": "Hybrid", "capacityMinutes": 600, "coordinationMinutes": 0,
             "estimates": [{"service": "coding", "minutes": 150}, {"service": "design", "minutes": 150},
                           {"service": "writing", "minutes": 150}]},
        ],
        deliverables=[
            {"id": "d-design",  "ownerId": "devco",    "service": "design"},
            {"id": "d-writing", "ownerId": "designco", "service": "writing"},
            {"id": "d-coding",  "ownerId": "copyco",   "service": "coding"},
            {"id": "d-code2",   "ownerId": "hybridco", "service": "coding"},
        ],
    )


def _five_business_scenario() -> Scenario:
    return _make_scenario(
        businesses=[
            {"id": "b1", "name": "B1", "capacityMinutes": 800, "coordinationMinutes": 0,
             "estimates": [{"service": "coding", "minutes": 100}, {"service": "design", "minutes": 300}]},
            {"id": "b2", "name": "B2", "capacityMinutes": 800, "coordinationMinutes": 0,
             "estimates": [{"service": "design", "minutes": 100}, {"service": "writing", "minutes": 300}]},
            {"id": "b3", "name": "B3", "capacityMinutes": 800, "coordinationMinutes": 0,
             "estimates": [{"service": "writing", "minutes": 100}, {"service": "coding", "minutes": 300}]},
            {"id": "b4", "name": "B4", "capacityMinutes": 800, "coordinationMinutes": 0,
             "estimates": [{"service": "coding", "minutes": 120}, {"service": "writing", "minutes": 300}]},
            {"id": "b5", "name": "B5", "capacityMinutes": 800, "coordinationMinutes": 0,
             "estimates": [{"service": "design", "minutes": 120}, {"service": "writing", "minutes": 300}]},
        ],
        deliverables=[
            {"id": "d1", "ownerId": "b1", "service": "design"},
            {"id": "d2", "ownerId": "b2", "service": "writing"},
            {"id": "d3", "ownerId": "b3", "service": "coding"},
            {"id": "d4", "ownerId": "b4", "service": "writing"},
            {"id": "d5", "ownerId": "b5", "service": "design"},
        ],
    )


def _oracle(scenario: Scenario):
    """Independent brute-force oracle: returns (status, optimal_total_effort|None)."""
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

    cap = {b.id: b.capacityMinutes for b in scenario.businesses}
    coord = {b.id: b.coordinationMinutes for b in scenario.businesses}
    total_baseline = sum(baselines.values())

    any_feasible = False
    best_key = None
    best_effort = None

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

        key = (total_effort, max(_ratio(b.id) for b in scenario.businesses), sorted(amap.items()))
        if best_key is None or key < best_key:
            best_key = key
            best_effort = total_effort

    if best_key is not None:
        return "collaboration", best_effort
    if any_feasible:
        return "no_exchange", None
    return "infeasible", None


# ===========================================================================
# MVP default preserved
# ===========================================================================

class TestMvpDefaultPreserved:

    def test_three_business_default_still_works(self):
        scenario = Scenario.model_validate(
            _fixtures.load("canonical.json")["scenario"]
        )
        # No config → historical behaviour
        assert optimize(scenario).status.value == "collaboration"
        # Explicit default config (MVP) → also works
        assert optimize(scenario, DEFAULT_CONFIG).status.value == "collaboration"

    def test_default_config_is_not_extended(self):
        assert DEFAULT_CONFIG.extended_mode is False
        assert DEFAULT_CONFIG.max_businesses == 3

    def test_four_business_rejected_in_mvp_mode(self):
        scenario = _four_business_scenario()
        with pytest.raises(ExtendedModeError, match="extended mode"):
            optimize(scenario, DEFAULT_CONFIG)

    def test_four_business_rejected_via_validate(self):
        with pytest.raises(ExtendedModeError):
            validate_scenario_bounds(_four_business_scenario(), DEFAULT_CONFIG)


# ===========================================================================
# Extended mode: 4 and 5 businesses
# ===========================================================================

class TestExtendedFourBusiness:

    def test_four_business_accepted_in_extended_mode(self):
        scenario = _four_business_scenario()
        result = optimize(scenario, EXTENDED)
        assert result.status.value in ("collaboration", "no_exchange", "infeasible")

    def test_four_business_matches_oracle_status(self):
        scenario = _four_business_scenario()
        result = optimize(scenario, EXTENDED)
        oracle_status, _ = _oracle(scenario)
        assert result.status.value == oracle_status

    def test_four_business_matches_oracle_effort(self):
        scenario = _four_business_scenario()
        result = optimize(scenario, EXTENDED)
        oracle_status, oracle_effort = _oracle(scenario)
        if result.status.value == "collaboration" and oracle_status == "collaboration":
            assert result.totals.effortMinutes == oracle_effort

    def test_four_business_certificate_verifies(self):
        from optimizer.solver import verify_certificate
        scenario = _four_business_scenario()
        result = optimize(scenario, EXTENDED)
        if result.status.value == "collaboration":
            vr = verify_certificate(scenario, result.assignments, result.certificate)
            assert vr.valid


class TestExtendedFiveBusiness:

    def test_five_business_accepted_in_extended_mode(self):
        scenario = _five_business_scenario()
        result = optimize(scenario, EXTENDED)
        assert result.status.value in ("collaboration", "no_exchange", "infeasible")

    def test_five_business_matches_oracle_status(self):
        scenario = _five_business_scenario()
        result = optimize(scenario, EXTENDED)
        oracle_status, _ = _oracle(scenario)
        assert result.status.value == oracle_status

    def test_five_business_matches_oracle_effort(self):
        scenario = _five_business_scenario()
        result = optimize(scenario, EXTENDED)
        oracle_status, oracle_effort = _oracle(scenario)
        if result.status.value == "collaboration" and oracle_status == "collaboration":
            assert result.totals.effortMinutes == oracle_effort

    def test_five_business_rejected_in_mvp(self):
        with pytest.raises(ExtendedModeError):
            optimize(_five_business_scenario(), DEFAULT_CONFIG)


# ===========================================================================
# Contract rejects 6 businesses / 7 deliverables at parse time
# ===========================================================================

class TestHardLimits:

    def test_six_businesses_rejected_by_contract(self):
        businesses = [
            {"id": f"b{i}", "name": f"B{i}", "capacityMinutes": 600, "coordinationMinutes": 0,
             "estimates": [{"service": "coding", "minutes": 100}]}
            for i in range(6)
        ]
        with pytest.raises(ValidationError):
            _make_scenario(businesses, [{"id": "d1", "ownerId": "b0", "service": "coding"}])

    def test_seven_deliverables_rejected_by_contract(self):
        businesses = [
            {"id": "b1", "name": "B1", "capacityMinutes": 600, "coordinationMinutes": 0,
             "estimates": [{"service": "coding", "minutes": 100}]},
            {"id": "b2", "name": "B2", "capacityMinutes": 600, "coordinationMinutes": 0,
             "estimates": [{"service": "coding", "minutes": 100}]},
            {"id": "b3", "name": "B3", "capacityMinutes": 600, "coordinationMinutes": 0,
             "estimates": [{"service": "coding", "minutes": 100}]},
        ]
        deliverables = [{"id": f"d{j}", "ownerId": "b1", "service": "coding"} for j in range(7)]
        with pytest.raises(ValidationError):
            _make_scenario(businesses, deliverables)


# ===========================================================================
# Candidate cap
# ===========================================================================

class TestCandidateCap:

    def test_candidate_count_within_cap_for_five_business(self):
        scenario = _five_business_scenario()
        bound = candidate_count_upper_bound(scenario)
        assert bound <= MAX_CANDIDATES

    def test_candidate_count_canonical(self):
        scenario = Scenario.model_validate(
            _fixtures.load("canonical.json")["scenario"]
        )
        # Each deliverable has 2 providers → 2^3 = 8
        assert candidate_count_upper_bound(scenario) == 8

    def test_max_candidates_is_15625(self):
        assert MAX_CANDIDATES == 15625


# ===========================================================================
# Bounded runtime observation (no guarantees)
# ===========================================================================

class TestRuntimeObservation:

    def test_five_business_runtime_recorded(self):
        """
        Record wall-clock time for a 5-business solve. This is an OBSERVATION
        on developer hardware, NOT a performance guarantee for Lambda.
        We assert only a generous upper bound to catch pathological regressions.
        """
        scenario = _five_business_scenario()
        start = time.perf_counter()
        optimize(scenario, EXTENDED)
        elapsed = time.perf_counter() - start
        # Generous ceiling — 5^5 candidates is trivial; this catches only gross regressions.
        assert elapsed < 5.0, f"5-business solve took {elapsed:.3f}s (regression?)"

    def test_repeated_five_business_deterministic(self):
        scenario = _five_business_scenario()
        r1 = optimize(scenario, EXTENDED)
        r2 = optimize(scenario, EXTENDED)
        assert r1.status == r2.status
        p1 = sorted((a.deliverableId, a.providerId) for a in r1.assignments)
        p2 = sorted((a.deliverableId, a.providerId) for a in r2.assignments)
        assert p1 == p2
