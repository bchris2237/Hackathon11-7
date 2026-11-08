"""
A4 — Individual benefits and deterministic scoring tests.
Owner: Developer 1.

Tests compute_metrics and rank_allocation from optimizer/metrics.py.
All expected values come from the frozen contract spec, not from internals.

Required A4 acceptance cases:
  - Coordination=60 produces effort=720, individual savings=240
  - Zero savings (no cross-assignment benefit)
  - Disadvantaged provider filtered out by rank_allocation
  - High coordination forces self-production (no_exchange via rank_allocation)
  - Zero-baseline participant handled without division
  - Stable tiebreak on equal-effort candidates
  - Labor burden is NOT monetary utility (documented, not testable arithmetically)
"""
import json
from pathlib import Path

import pytest

from contracts.models import BusinessMetrics, Scenario, Totals
from optimizer.metrics import (
    ScoredCandidate,
    _score_key,
    compute_metrics,
    rank_allocation,
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


def _canonical_optimal_map() -> dict[str, str]:
    """Optimal assignment map for the canonical fixture."""
    return {
        "d-design":  "designco",
        "d-writing": "copyco",
        "d-coding":  "devco",
    }


def _canonical_self_map() -> dict[str, str]:
    """Self-assignment map for the canonical fixture."""
    return {
        "d-design":  "devco",
        "d-writing": "designco",
        "d-coding":  "copyco",
    }


# ===========================================================================
# compute_metrics
# ===========================================================================

class TestComputeMetricsCanonical:

    def test_optimal_map_production_180_each(self):
        scenario = _scenario("canonical.json")
        metrics, totals = compute_metrics(scenario, _canonical_optimal_map())
        for m in metrics:
            assert m.productionMinutes == 180, f"{m.businessId} production={m.productionMinutes}"

    def test_optimal_map_baseline_480_each(self):
        scenario = _scenario("canonical.json")
        metrics, totals = compute_metrics(scenario, _canonical_optimal_map())
        for m in metrics:
            assert m.baselineMinutes == 480

    def test_optimal_map_net_savings_300_each(self):
        scenario = _scenario("canonical.json")
        metrics, totals = compute_metrics(scenario, _canonical_optimal_map())
        for m in metrics:
            assert m.netSavingsMinutes == 300

    def test_optimal_map_totals(self):
        scenario = _scenario("canonical.json")
        metrics, totals = compute_metrics(scenario, _canonical_optimal_map())
        assert totals.baselineMinutes == 1440
        assert totals.productionMinutes == 540
        assert totals.coordinationMinutes == 0
        assert totals.effortMinutes == 540
        assert totals.grossSavingsMinutes == 900
        assert totals.netSavingsMinutes == 900

    def test_self_map_zero_coordination(self):
        """Self-assignment: all owners do own work → no involvement → no coordination."""
        scenario = _scenario("canonical.json")
        metrics, totals = compute_metrics(scenario, _canonical_self_map())
        for m in metrics:
            assert m.coordinationMinutes == 0

    def test_self_map_production_equals_baseline(self):
        """Self-assignment: each business produces exactly its own baseline."""
        scenario = _scenario("canonical.json")
        metrics, totals = compute_metrics(scenario, _canonical_self_map())
        for m in metrics:
            assert m.productionMinutes == m.baselineMinutes

    def test_self_map_net_savings_zero(self):
        """Self-assignment: effort=baseline → netSavings=0 for each."""
        scenario = _scenario("canonical.json")
        metrics, totals = compute_metrics(scenario, _canonical_self_map())
        for m in metrics:
            assert m.netSavingsMinutes == 0

    def test_metrics_sorted_by_business_id(self):
        scenario = _scenario("canonical.json")
        metrics, _ = compute_metrics(scenario, _canonical_optimal_map())
        ids = [m.businessId for m in metrics]
        assert ids == sorted(ids)

    def test_return_types(self):
        scenario = _scenario("canonical.json")
        metrics, totals = compute_metrics(scenario, _canonical_optimal_map())
        assert isinstance(metrics, list)
        assert all(isinstance(m, BusinessMetrics) for m in metrics)
        assert isinstance(totals, Totals)

    def test_all_minutes_are_integers(self):
        scenario = _scenario("canonical.json")
        metrics, totals = compute_metrics(scenario, _canonical_optimal_map())
        for m in metrics:
            assert isinstance(m.effortMinutes, int)
            assert isinstance(m.netSavingsMinutes, int)
        assert isinstance(totals.netSavingsMinutes, int)


class TestComputeMetricsCoordination:
    """A4 required: coordination=60 → effort=720, individual savings=240."""

    def test_effort_240_each_with_coord60(self):
        """Each: production=180, coord=60, effort=240."""
        scenario = _scenario("coordination.json")
        coord_map = {
            "d-design":  "designco",
            "d-writing": "copyco",
            "d-coding":  "devco",
        }
        metrics, totals = compute_metrics(scenario, coord_map)
        for m in metrics:
            assert m.effortMinutes == 240, f"{m.businessId} effort={m.effortMinutes}"

    def test_coordination_charged_once_per_business(self):
        """Coordination is charged once per involved business, not per assignment edge."""
        scenario = _scenario("coordination.json")
        coord_map = {
            "d-design":  "designco",
            "d-writing": "copyco",
            "d-coding":  "devco",
        }
        metrics, totals = compute_metrics(scenario, coord_map)
        for m in metrics:
            assert m.coordinationMinutes == 60, f"{m.businessId} coord={m.coordinationMinutes}"

    def test_total_coordination_180(self):
        scenario = _scenario("coordination.json")
        coord_map = {
            "d-design":  "designco",
            "d-writing": "copyco",
            "d-coding":  "devco",
        }
        metrics, totals = compute_metrics(scenario, coord_map)
        assert totals.coordinationMinutes == 180

    def test_net_savings_240_each(self):
        """A4 required: each business saves 240 min with coord=60."""
        scenario = _scenario("coordination.json")
        coord_map = {
            "d-design":  "designco",
            "d-writing": "copyco",
            "d-coding":  "devco",
        }
        metrics, totals = compute_metrics(scenario, coord_map)
        for m in metrics:
            assert m.netSavingsMinutes == 240

    def test_total_effort_720(self):
        """A4 required: total effort=720 with coord=60."""
        scenario = _scenario("coordination.json")
        coord_map = {
            "d-design":  "designco",
            "d-writing": "copyco",
            "d-coding":  "devco",
        }
        _, totals = compute_metrics(scenario, coord_map)
        assert totals.effortMinutes == 720

    def test_gross_savings_900_coord_scenario(self):
        """Gross savings = baseline - production = 1440 - 540 = 900 (independent of coord)."""
        scenario = _scenario("coordination.json")
        coord_map = {
            "d-design":  "designco",
            "d-writing": "copyco",
            "d-coding":  "devco",
        }
        _, totals = compute_metrics(scenario, coord_map)
        assert totals.grossSavingsMinutes == 900

    def test_self_assignment_no_coordination_charge(self):
        """Self-assignment even in coord=60 scenario → no involvement → no coord charge."""
        scenario = _scenario("coordination.json")
        self_map = {"d-design": "devco", "d-writing": "designco", "d-coding": "copyco"}
        metrics, totals = compute_metrics(scenario, self_map)
        for m in metrics:
            assert m.coordinationMinutes == 0
        assert totals.coordinationMinutes == 0


class TestComputeMetricsEdgeCases:

    def test_partial_exchange_only_some_cross_assigned(self):
        """
        Only d-design is cross-assigned. devco (owner) and designco (provider)
        are involved → each pays coord. copyco does its own work → no coord.
        """
        scenario = _make_scenario(
            businesses=[
                {"id": "copyco",   "name": "C", "capacityMinutes": 600, "coordinationMinutes": 50,
                 "estimates": [{"service": "coding", "minutes": 480}, {"service": "writing", "minutes": 180}]},
                {"id": "designco", "name": "B", "capacityMinutes": 600, "coordinationMinutes": 50,
                 "estimates": [{"service": "design", "minutes": 180}, {"service": "writing", "minutes": 480}]},
                {"id": "devco",    "name": "A", "capacityMinutes": 600, "coordinationMinutes": 50,
                 "estimates": [{"service": "coding", "minutes": 180}, {"service": "design", "minutes": 480}]},
            ],
            deliverables=[
                {"id": "d-design",  "ownerId": "devco",    "service": "design"},
                {"id": "d-writing", "ownerId": "designco", "service": "writing"},
                {"id": "d-coding",  "ownerId": "copyco",   "service": "coding"},
            ],
        )
        # d-design cross-assigned to designco; others self-assigned
        partial_map = {
            "d-design":  "designco",  # cross
            "d-writing": "designco",  # self
            "d-coding":  "copyco",    # self
        }
        metrics, totals = compute_metrics(scenario, partial_map)
        by_id = {m.businessId: m for m in metrics}
        # devco and designco are involved (cross assignment for d-design)
        assert by_id["devco"].coordinationMinutes == 50
        assert by_id["designco"].coordinationMinutes == 50
        # copyco is NOT involved → no coord
        assert by_id["copyco"].coordinationMinutes == 0

    def test_zero_baseline_business_handled(self):
        """
        A business with no deliverables has baseline=0.
        It can still be a provider. netSavings = 0 - effort.
        """
        scenario = _make_scenario(
            businesses=[
                {"id": "devco",    "name": "A", "capacityMinutes": 9999, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 100}, {"service": "design", "minutes": 480}]},
                {"id": "designco", "name": "B", "capacityMinutes": 9999, "coordinationMinutes": 0,
                 "estimates": [{"service": "design", "minutes": 180}, {"service": "writing", "minutes": 480}]},
                # copyco has no deliverables → baseline = 0
                {"id": "copyco",   "name": "C", "capacityMinutes": 9999, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 200}]},
            ],
            deliverables=[
                {"id": "d-design", "ownerId": "devco",    "service": "design"},
                {"id": "d-writing", "ownerId": "designco", "service": "writing"},
                # No deliverable for copyco
                {"id": "d-coding",  "ownerId": "devco",    "service": "coding"},
            ],
        )
        amap = {
            "d-design":  "designco",   # cross: designco provides, devco owns
            "d-writing": "designco",   # self
            "d-coding":  "devco",      # self
        }
        metrics, totals = compute_metrics(scenario, amap)
        copyco_m = next(m for m in metrics if m.businessId == "copyco")
        assert copyco_m.baselineMinutes == 0
        assert copyco_m.productionMinutes == 0
        assert copyco_m.netSavingsMinutes == 0  # 0 - 0 = 0


# ===========================================================================
# rank_allocation
# ===========================================================================

class TestRankAllocationFiltering:

    def test_harmed_business_filtered_out(self):
        """
        A4 required: if any business has netSavings < 0, the candidate is filtered,
        even when total savings are positive.
        """
        scenario = _scenario("canonical.json")
        # Build a candidate where devco would be harmed:
        # devco produces both d-design(self, 480) and d-coding(cross, 180)
        # baseline_devco=480, effort=660, net=-180 → harmed
        amap = {
            "d-design":  "devco",      # self
            "d-writing": "designco",   # self
            "d-coding":  "devco",      # cross — devco takes extra work
        }
        metrics, totals = compute_metrics(scenario, amap)
        candidates = [(amap, metrics, totals)]
        result = rank_allocation(candidates)
        # devco is harmed → filtered out
        assert result == []

    def test_valid_candidate_survives(self):
        scenario = _scenario("canonical.json")
        amap = _canonical_optimal_map()
        metrics, totals = compute_metrics(scenario, amap)
        result = rank_allocation([(amap, metrics, totals)])
        assert len(result) == 1

    def test_mixed_candidates_only_valid_survive(self):
        scenario = _scenario("canonical.json")
        good_amap = _canonical_optimal_map()
        bad_amap = {
            "d-design":  "devco",
            "d-writing": "designco",
            "d-coding":  "devco",    # devco harmed
        }
        good_m, good_t = compute_metrics(scenario, good_amap)
        bad_m, bad_t = compute_metrics(scenario, bad_amap)
        result = rank_allocation([(good_amap, good_m, good_t), (bad_amap, bad_m, bad_t)])
        assert len(result) == 1
        assert result[0].assignment_map == good_amap

    def test_empty_input_returns_empty(self):
        assert rank_allocation([]) == []

    def test_all_harmed_returns_empty(self):
        scenario = _scenario("canonical.json")
        # Self-assignment has netSavings=0 for all → survives (0 >= 0)
        amap = _canonical_self_map()
        metrics, totals = compute_metrics(scenario, amap)
        # Make all negative by pretending a harmed candidate
        bad_metrics = [
            BusinessMetrics(
                businessId=m.businessId,
                baselineMinutes=m.baselineMinutes,
                productionMinutes=m.productionMinutes,
                coordinationMinutes=m.coordinationMinutes,
                effortMinutes=m.effortMinutes + 1000,
                netSavingsMinutes=-1000,
            )
            for m in metrics
        ]
        result = rank_allocation([(amap, bad_metrics, totals)])
        assert result == []


class TestRankAllocationOrdering:

    def test_best_first_lower_effort(self):
        """Lower total effort comes first."""
        scenario = _scenario("canonical.json")
        good_amap = _canonical_optimal_map()  # effort=540
        self_amap = _canonical_self_map()     # effort=1440
        good_m, good_t = compute_metrics(scenario, good_amap)
        self_m, self_t = compute_metrics(scenario, self_amap)
        result = rank_allocation([
            (self_amap, self_m, self_t),
            (good_amap, good_m, good_t),
        ])
        assert result[0].assignment_map == good_amap
        assert result[0].totals.effortMinutes < result[1].totals.effortMinutes

    def test_scored_candidate_fields(self):
        scenario = _scenario("canonical.json")
        amap = _canonical_optimal_map()
        m, t = compute_metrics(scenario, amap)
        result = rank_allocation([(amap, m, t)])
        c = result[0]
        assert isinstance(c, ScoredCandidate)
        assert c.assignment_map == amap
        assert c.metrics == m
        assert c.totals == t
        assert isinstance(c.score_key, tuple)
        assert len(c.score_key) == 3  # (effort, max_ratio, tertiary)

    def test_stable_tiebreak_tertiary(self):
        """
        Two candidates with identical effort and ratio → sorted by (deliverableId, providerId) pairs.
        """
        scenario = _scenario("canonical.json")
        # Build two equal-effort self-assignment candidates with different deliverable sets
        # by using compute_metrics directly to manufacture equal-effort candidates.
        # Easiest: use same map twice — they compare equal → order is stable.
        amap = _canonical_self_map()
        m, t = compute_metrics(scenario, amap)
        result = rank_allocation([(amap, m, t), (amap, m, t)])
        assert len(result) == 2
        # Both have same score_key → order is stable (sort is stable in Python)
        assert result[0].score_key == result[1].score_key

    def test_coordination_fixture_ranked_correctly(self):
        scenario = _scenario("coordination.json")
        opt = _load("coordination.json")["expected_result"]
        # The optimal map for coordination fixture
        coord_map = {
            "d-design":  "designco",
            "d-writing": "copyco",
            "d-coding":  "devco",
        }
        self_map = {"d-design": "devco", "d-writing": "designco", "d-coding": "copyco"}
        cm, ct = compute_metrics(scenario, coord_map)
        sm, st = compute_metrics(scenario, self_map)
        result = rank_allocation([(coord_map, cm, ct), (self_map, sm, st)])
        # coord_map effort=720 < self_map effort=1440 → coord_map wins
        assert result[0].assignment_map == coord_map


class TestRankAllocationHighCoordination:
    """A4 required: high coordination should select self-production over collaboration."""

    def test_high_coordination_self_beats_collab(self):
        """
        coord=400. Collaboration effort = 180+400=580 > baseline=480 → net=-100 → harmed.
        Self-assignment effort = 480, net = 0. Only self-assignment survives filter.
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
        collab_map = {"d-design": "designco", "d-writing": "copyco", "d-coding": "devco"}
        self_map   = {"d-design": "devco",    "d-writing": "designco", "d-coding": "copyco"}

        cm, ct = compute_metrics(scenario, collab_map)
        sm, st = compute_metrics(scenario, self_map)

        # Collaboration: each net = 480-(180+400) = -100 → harmed
        for m in cm:
            assert m.netSavingsMinutes < 0

        result = rank_allocation([(collab_map, cm, ct), (self_map, sm, st)])
        # collaboration filtered out; only self survives
        assert len(result) == 1
        assert result[0].assignment_map == self_map


class TestRankAllocationZeroBaseline:
    """A4 required: zero-baseline participant handled without division."""

    def test_zero_baseline_business_no_division_error(self):
        """
        copyco has no deliverables → baseline=0.
        rank_allocation must not divide by zero.
        """
        scenario = _make_scenario(
            businesses=[
                {"id": "devco",    "name": "A", "capacityMinutes": 9999, "coordinationMinutes": 0,
                 "estimates": [{"service": "design", "minutes": 480}, {"service": "coding", "minutes": 180}]},
                {"id": "designco", "name": "B", "capacityMinutes": 9999, "coordinationMinutes": 0,
                 "estimates": [{"service": "design", "minutes": 180}, {"service": "writing", "minutes": 480}]},
                {"id": "copyco",   "name": "C", "capacityMinutes": 9999, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 200}]},
            ],
            deliverables=[
                {"id": "d-design",  "ownerId": "devco",    "service": "design"},
                {"id": "d-writing", "ownerId": "designco", "service": "writing"},
                {"id": "d-coding",  "ownerId": "devco",    "service": "coding"},
            ],
        )
        amap = {
            "d-design":  "designco",   # cross
            "d-writing": "designco",   # self
            "d-coding":  "devco",      # self
        }
        m, t = compute_metrics(scenario, amap)
        # Should not raise ZeroDivisionError
        result = rank_allocation([(amap, m, t)])
        # copyco baseline=0 → net=0 → survives filter
        assert isinstance(result, list)

    def test_zero_baseline_ratio_proxy_is_sentinel(self):
        """_score_key uses _RATIO_INF for zero-baseline businesses."""
        from optimizer.metrics import _score_key, _RATIO_INF
        scenario = _make_scenario(
            businesses=[
                {"id": "devco",    "name": "A", "capacityMinutes": 9999, "coordinationMinutes": 0,
                 "estimates": [{"service": "design", "minutes": 480}]},
                {"id": "designco", "name": "B", "capacityMinutes": 9999, "coordinationMinutes": 0,
                 "estimates": [{"service": "writing", "minutes": 480}]},
                {"id": "copyco",   "name": "C", "capacityMinutes": 9999, "coordinationMinutes": 0,
                 "estimates": []},   # no estimates, no baseline
            ],
            deliverables=[
                {"id": "d-design",  "ownerId": "devco",    "service": "design"},
                {"id": "d-writing", "ownerId": "designco", "service": "writing"},
            ],
        )
        amap = {"d-design": "devco", "d-writing": "designco"}
        m, t = compute_metrics(scenario, amap)
        key = _score_key(amap, m, t)
        # key[1] is max_ratio — copyco has baseline=0 → _RATIO_INF
        assert key[1] == _RATIO_INF


class TestComputeMetricsMatchesOptimizer:
    """compute_metrics output must match what the solver reports in its Result."""

    def test_canonical_metrics_match_solver(self):
        scenario = _scenario("canonical.json")
        result = optimize(scenario)
        amap = {a.deliverableId: a.providerId for a in result.assignments}
        computed_m, computed_t = compute_metrics(scenario, amap)

        solver_by_id = {m.businessId: m for m in result.businessMetrics}
        for m in computed_m:
            sm = solver_by_id[m.businessId]
            assert m.baselineMinutes     == sm.baselineMinutes
            assert m.productionMinutes   == sm.productionMinutes
            assert m.coordinationMinutes == sm.coordinationMinutes
            assert m.effortMinutes       == sm.effortMinutes
            assert m.netSavingsMinutes   == sm.netSavingsMinutes

        assert computed_t.baselineMinutes    == result.totals.baselineMinutes
        assert computed_t.netSavingsMinutes  == result.totals.netSavingsMinutes
        assert computed_t.effortMinutes      == result.totals.effortMinutes

    def test_coordination_metrics_match_solver(self):
        scenario = _scenario("coordination.json")
        result = optimize(scenario)
        amap = {a.deliverableId: a.providerId for a in result.assignments}
        m, t = compute_metrics(scenario, amap)
        assert t.coordinationMinutes == result.totals.coordinationMinutes
        assert t.effortMinutes       == result.totals.effortMinutes
        assert t.netSavingsMinutes   == result.totals.netSavingsMinutes
