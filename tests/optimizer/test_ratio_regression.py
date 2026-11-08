"""Regression tests found by comparing the optimizer with an independent brute-force reference on random scenarios.

The contract's second tie-break is the smallest WORST individual effort/baseline ratio (integer cross multiplication, zero baseline
handled explicitly). An earlier proxy (effort * total_baseline) ordered businesses by raw effort, not by ratio, so two plans with
the same total effort could be ranked wrongly. These tests pin the correct behaviour with first-principles numbers.
"""

from __future__ import annotations

import json
from fractions import Fraction
from itertools import product

import pytest

from optimizer.contract import Scenario
from optimizer.ratio import RatioRank, ratio_rank
from optimizer.solver import solve

# Found by the differential run (seed 20261107). The old proxy chose a plan whose worst share was larger than another plan's with
# the SAME total effort. Expected values below are derived here by brute force from the scenario, not copied from solver output.
SCENARIO = json.loads('{"schemaVersion":"1","businesses":[{"id":"alpha","name":"Alpha","capacityMinutes":100,"coordinationMinutes":0,"estimates":[{"service":"coding","minutes":240}]},{"id":"bravo","name":"Bravo","capacityMinutes":1500,"coordinationMinutes":60,"estimates":[{"service":"writing","minutes":60},{"service":"coding","minutes":60},{"service":"design","minutes":480}]},{"id":"charlie","name":"Charlie","capacityMinutes":900,"coordinationMinutes":200,"estimates":[{"service":"writing","minutes":480},{"service":"design","minutes":60},{"service":"coding","minutes":60}]}],"deliverables":[{"id":"d0","ownerId":"bravo","service":"design"},{"id":"d1","ownerId":"charlie","service":"writing"},{"id":"d2","ownerId":"alpha","service":"coding"},{"id":"d3","ownerId":"charlie","service":"writing"},{"id":"d4","ownerId":"charlie","service":"writing"},{"id":"d5","ownerId":"charlie","service":"coding"}],"confirmed":true}')


def _best_by_exact_rules(scenario: dict):
    """Independent brute force: least total effort, then least worst share (Fraction), then provider ids. Returns (assignment, worst share)."""
    est = {b['id']: {e['service']: e['minutes'] for e in b['estimates']} for b in scenario['businesses']}
    cap = {b['id']: b['capacityMinutes'] for b in scenario['businesses']}
    coord = {b['id']: b['coordinationMinutes'] for b in scenario['businesses']}
    dels = sorted(scenario['deliverables'], key=lambda d: d['id'])
    base = {b: 0 for b in est}
    for d in dels:
        base[d['ownerId']] += est[d['ownerId']][d['service']]
    options = [[b for b in est if d['service'] in est[b]] for d in dels]
    best = None
    for combo in product(*options):
        prod = {b: 0 for b in est}
        involved = set()
        for d, p in zip(dels, combo):
            prod[p] += est[p][d['service']]
            if p != d['ownerId']:
                involved |= {p, d['ownerId']}
        effort = {b: prod[b] + (coord[b] if b in involved else 0) for b in est}
        if any(effort[b] > cap[b] or base[b] - effort[b] < 0 for b in est):
            continue
        shares = [Fraction(effort[b], base[b]) for b in est if base[b]]
        key = (sum(effort.values()), max(shares, default=Fraction(0)), [(d['id'], p) for d, p in zip(dels, combo)])
        if best is None or key < best:
            best = key
    return best


def test_equal_total_effort_prefers_the_smaller_worst_share():
    expected_effort, expected_worst, expected_pairs = _best_by_exact_rules(SCENARIO)
    result = solve(Scenario.model_validate(SCENARIO))
    assert result.status.value == 'collaboration' and result.totals is not None and result.businessMetrics is not None
    assert result.totals.effortMinutes == expected_effort
    assert max(Fraction(m.effortMinutes, m.baselineMinutes) for m in result.businessMetrics if m.baselineMinutes) == expected_worst
    assert [(a.deliverableId, a.providerId) for a in sorted(result.assignments, key=lambda a: a.deliverableId)] == expected_pairs


@pytest.mark.parametrize(
    ('small', 'large'),
    [((1, 2), (5, 8)), ((300, 480), (320, 480)), ((0, 5), (1, 1000)), ((7, 3), (9, 3)), ((333, 1000), (1, 3))],
)
def test_ratio_rank_orders_exactly(small, large):
    assert Fraction(*small) < Fraction(*large)
    a, b = ratio_rank(*small), ratio_rank(*large)
    assert a < b and b > a and not b < a and a != b


def test_equal_fractions_with_different_numerators_are_equal():
    assert ratio_rank(1, 2) == ratio_rank(50, 100) and hash(ratio_rank(1, 2)) == hash(ratio_rank(1, 2))
    assert not ratio_rank(1, 2) < ratio_rank(50, 100) and not ratio_rank(50, 100) < ratio_rank(1, 2)


def test_zero_baseline_is_the_largest_share_and_ties_with_itself():
    assert ratio_rank(5, 0) > ratio_rank(10**12, 1)
    assert ratio_rank(0, 0) == ratio_rank(999, 0)
    assert not ratio_rank(5, 0) < ratio_rank(5, 0)


def test_the_comparison_is_exact_where_a_float_would_round():
    # 1/3 and 333333333333333/999999999999999 are equal as rationals; the comparison uses integers only
    assert ratio_rank(1, 3) == ratio_rank(333_333_333_333_333, 999_999_999_999_999)
    assert ratio_rank(10**15, 10**15 + 1) < ratio_rank(10**15 + 1, 10**15 + 2)


def test_negative_values_are_refused():
    with pytest.raises(ValueError):
        RatioRank(-1, 5)
    with pytest.raises(ValueError):
        RatioRank(1, -5)
