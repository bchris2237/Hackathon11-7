"""
TradeMind v1 — individual benefits and deterministic scoring.
Owner: Developer 1 (optimizer/** and tests/optimizer/**).

Public surface (A4):
  compute_metrics(scenario, assignment_map) → (list[BusinessMetrics], Totals)
  rank_allocation(candidates)              → list[ScoredCandidate]

Design
------
compute_metrics
  Applies the frozen formulas from the contract spec to one specific assignment
  map and returns per-business metrics and totals.  It does NOT evaluate
  feasibility — that is the caller's job (via validate_assignment / engine).
  This function is the single authoritative implementation of the accounting
  formulas.  engine._evaluate_candidate uses equivalent logic; they share
  the same derivation from the spec, not shared code.

  Formulas (all integer minutes, no floats):
    baseline_i         = sum of owner's own estimates for own deliverables
    production_i       = sum of provider's estimate(service) for each deliverable assigned to i
    involved           = business i is in an exchange iff it provides to a deliverable
                         it doesn't own, OR any of its own deliverables is assigned to
                         someone else
    coordinationCharge_i = i.coordinationMinutes  if i is involved,  else 0
    effort_i           = production_i + coordinationCharge_i
    netSavings_i       = baseline_i - effort_i

    total_baseline     = sum(baseline_i)
    total_production   = sum(production_i)
    total_coordination = sum(coordinationCharge_i)
    total_effort       = sum(effort_i)
    gross_savings      = total_baseline - total_production
    net_savings        = total_baseline - total_effort

  Important model limitation (must be stated clearly):
    Labor burden is modeled as minutes of effort, NOT monetary utility.
    Savings are reductions in labor time, not profit.  Two businesses saving
    equal minutes may have very different cash implications depending on wage
    rates, opportunity costs and market pricing — none of which are modeled here.

rank_allocation
  Accepts a list of (assignment_map, BusinessMetrics list, Totals) triples
  and returns them sorted from best to worst by the deterministic tiebreak:

    Primary (ascending):   total effortMinutes
    Secondary (ascending): max effort/baseline ratio across all businesses,
                           using integer cross-multiplication (no division).
                           For business i: proxy = effort_i × total_baseline_global
                           where total_baseline_global = sum of all baselines.
                           baseline_i == 0 → proxy = MAX_INT (effectively infinite ratio).
    Tertiary (ascending):  sorted [(deliverableId, providerId), ...] lexicographically.

  Filters OUT candidates where any business has netSavings < 0, even if
  total net savings are positive.  This enforces the no-worse-off constraint
  at the scoring stage and prevents the best-looking allocations from
  silently harming a participant.

  Returns a list of ScoredCandidate named tuples in best-first order.
  Empty list if no candidates survive the no-worse-off filter.

All arithmetic is integer minutes.  No floats.  No monetary interpretation.
"""
from __future__ import annotations

from typing import NamedTuple

from contracts.models import (
    Assignment,
    BusinessMetrics,
    Scenario,
    ServiceName,
    Totals,
)
from optimizer.errors import MissingBaselineError

# Sentinel for zero-baseline ratio proxy (effectively infinite)
_RATIO_INF: int = 10 ** 15


# ---------------------------------------------------------------------------
# ScoredCandidate — the output unit of rank_allocation
# ---------------------------------------------------------------------------

class ScoredCandidate(NamedTuple):
    """
    A feasible, no-worse-off candidate assignment with its computed score key.

    Fields:
      assignment_map  — {deliverableId: providerId}
      metrics         — per-business metrics list (sorted by businessId)
      totals          — aggregate totals
      score_key       — (total_effort, max_ratio_proxy, tertiary_pairs)
                        lower is better
    """
    assignment_map: dict[str, str]
    metrics: list[BusinessMetrics]
    totals: Totals
    score_key: tuple


# ---------------------------------------------------------------------------
# compute_metrics
# ---------------------------------------------------------------------------

def compute_metrics(
    scenario: Scenario,
    assignment_map: dict[str, str],  # {deliverableId: providerId}
) -> tuple[list[BusinessMetrics], Totals]:
    """
    Compute per-business metrics and aggregate totals for one assignment map.

    Does NOT check feasibility — caller is responsible.
    Raises MissingBaselineError if any owner lacks an estimate for its deliverable.
    Raises KeyError if any providerId or deliverableId is not in the scenario
    (validate assignment IDs before calling).

    Returns (metrics sorted by businessId ascending, totals).
    """
    # Build index structures
    estimates: dict[str, dict[ServiceName, int]] = {
        b.id: {e.service: e.minutes for e in b.estimates}
        for b in scenario.businesses
    }
    service_map: dict[str, ServiceName] = {d.id: d.service for d in scenario.deliverables}
    owner_map: dict[str, str] = {d.id: d.ownerId for d in scenario.deliverables}

    # Baseline — raises MissingBaselineError if any owner estimate is absent
    baselines = _compute_baselines_local(scenario, estimates)

    # Production: for each deliverable, add the provider's estimate to its total
    production: dict[str, int] = {b.id: 0 for b in scenario.businesses}
    for d_id, provider_id in assignment_map.items():
        svc = service_map[d_id]          # KeyError if unknown deliverable
        production[provider_id] += estimates[provider_id][svc]  # KeyError if unknown provider

    # Involvement: charge coordination once per involved business
    involved: set[str] = set()
    for d_id, provider_id in assignment_map.items():
        if provider_id != owner_map[d_id]:
            involved.add(provider_id)
            involved.add(owner_map[d_id])

    # Effort and coordination charge
    coord_charge: dict[str, int] = {}
    effort: dict[str, int] = {}
    for b in scenario.businesses:
        c = b.coordinationMinutes if b.id in involved else 0
        coord_charge[b.id] = c
        effort[b.id] = production[b.id] + c

    # Per-business metrics (sorted by businessId for stable output)
    metrics: list[BusinessMetrics] = [
        BusinessMetrics(
            businessId=b.id,
            baselineMinutes=baselines[b.id],
            productionMinutes=production[b.id],
            coordinationMinutes=coord_charge[b.id],
            effortMinutes=effort[b.id],
            netSavingsMinutes=baselines[b.id] - effort[b.id],
        )
        for b in sorted(scenario.businesses, key=lambda x: x.id)
    ]

    # Totals
    total_baseline    = sum(baselines[b.id]     for b in scenario.businesses)
    total_production  = sum(production[b.id]    for b in scenario.businesses)
    total_coord       = sum(coord_charge[b.id]  for b in scenario.businesses)
    total_effort      = sum(effort[b.id]        for b in scenario.businesses)
    gross_savings     = total_baseline - total_production
    net_savings       = total_baseline - total_effort

    totals = Totals(
        baselineMinutes=total_baseline,
        productionMinutes=total_production,
        coordinationMinutes=total_coord,
        effortMinutes=total_effort,
        grossSavingsMinutes=gross_savings,
        netSavingsMinutes=net_savings,
    )

    return metrics, totals


# ---------------------------------------------------------------------------
# rank_allocation
# ---------------------------------------------------------------------------

def rank_allocation(
    candidates: list[tuple[dict[str, str], list[BusinessMetrics], Totals]],
) -> list[ScoredCandidate]:
    """
    Score and rank a list of (assignment_map, metrics, totals) candidates.

    Filters out any candidate where any business has netSavingsMinutes < 0.
    This enforces the no-worse-off constraint: total savings being positive
    does not compensate a harmed individual business.

    Remaining candidates are sorted by the deterministic tiebreak key:
      1. Ascending total effortMinutes.
      2. Ascending max(effort_i × total_baseline) — integer proxy for
         max effort/baseline ratio.  baseline_i == 0 → proxy = 10^15.
      3. Ascending sorted [(deliverableId, providerId), ...] pairs.

    Returns ScoredCandidate list, best first.
    Empty list if no candidates survive the no-worse-off filter.

    Labor burden is effort minutes, not monetary utility.
    """
    scored: list[ScoredCandidate] = []

    for assignment_map, metrics, totals in candidates:
        # No-worse-off filter: reject if ANY business has negative net savings
        if any(m.netSavingsMinutes < 0 for m in metrics):
            continue

        key = _score_key(assignment_map, metrics, totals)
        scored.append(ScoredCandidate(
            assignment_map=assignment_map,
            metrics=metrics,
            totals=totals,
            score_key=key,
        ))

    scored.sort(key=lambda c: c.score_key)
    return scored


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_baselines_local(
    scenario: Scenario,
    estimates: dict[str, dict[ServiceName, int]],
) -> dict[str, int]:
    """Baseline computation — local copy to avoid circular import with engine."""
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


def _score_key(
    assignment_map: dict[str, str],
    metrics: list[BusinessMetrics],
    totals: Totals,
) -> tuple:
    """
    Deterministic tiebreak key.  Lower is better.

    Primary:   total effortMinutes.
    Secondary: max(effort_i × total_baseline) as integer proxy for
               max(effort_i / baseline_i).  Zero baseline → _RATIO_INF.
    Tertiary:  sorted [(deliverableId, providerId)] pairs lexicographically.
    """
    total_baseline = totals.baselineMinutes

    def _ratio_proxy(m: BusinessMetrics) -> int:
        if m.baselineMinutes == 0:
            return _RATIO_INF
        return m.effortMinutes * total_baseline

    max_ratio = max((_ratio_proxy(m) for m in metrics), default=0)
    tertiary = sorted(assignment_map.items())
    return (totals.effortMinutes, max_ratio, tertiary)
