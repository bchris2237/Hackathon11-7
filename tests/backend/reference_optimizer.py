"""SIMULATED stand-in for Developer 1's optimizer, for backend tests only.

This is NOT the production optimizer and is never imported by production code. It exists so B4 can test the API
seam, the result checks and /explain before `optimizer.api` is merged. It implements the frozen contract's rules by
brute force (at most 3 providers per deliverable and 6 deliverables = 729 combinations), written independently of
backend/optimization.py's checker so the two can cross-check each other:

  * providers for a deliverable = businesses with an estimate for its service (missing = unsupported, never zero);
  * baseline = sum of the owner's own estimates for its own deliverables;
  * a business that provides to, or receives from, ANOTHER business is charged its coordinationMinutes once;
  * feasible = every business has effort <= capacity AND net savings >= 0;
  * best = least total effort, then the smallest worst-case effort/baseline ratio (integer cross multiplication via
    Fraction), then lexicographically smallest provider ids in deliverable-id order;
  * status: collaboration if the best allocation has a cross-business assignment and total net savings > 0;
    no_exchange if a feasible allocation exists but not a strictly beneficial exchange; infeasible if none exists.

Digests: SHA-256 of canonical JSON (sorted keys, no whitespace, UTF-8).
"""

from __future__ import annotations

import hashlib
import itertools
import json
from fractions import Fraction
from typing import Any


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def reference_optimize(scenario: dict[str, Any]) -> dict[str, Any]:
    businesses = {b["id"]: b for b in scenario["businesses"]}
    estimates = {bid: {e["service"]: e["minutes"] for e in b["estimates"]} for bid, b in businesses.items()}
    deliverables = sorted(scenario["deliverables"], key=lambda d: d["id"])

    baseline = dict.fromkeys(businesses, 0)
    for d in deliverables:
        baseline[d["ownerId"]] += estimates[d["ownerId"]][d["service"]]

    options = [sorted(bid for bid in businesses if d["service"] in estimates[bid]) for d in deliverables]
    best: tuple | None = None
    for combo in itertools.product(*options):
        production = dict.fromkeys(businesses, 0)
        exchanging: set[str] = set()
        for d, provider in zip(deliverables, combo, strict=True):
            production[provider] += estimates[provider][d["service"]]
            if provider != d["ownerId"]:
                exchanging.update((provider, d["ownerId"]))
        effort = {bid: production[bid] + (businesses[bid]["coordinationMinutes"] if bid in exchanging else 0) for bid in businesses}
        if any(effort[bid] > businesses[bid]["capacityMinutes"] or baseline[bid] - effort[bid] < 0 for bid in businesses):
            continue
        ratios = [Fraction(effort[bid], baseline[bid]) if baseline[bid] else Fraction(0) for bid in businesses]
        key = (sum(effort.values()), max(ratios), combo)
        if best is None or key < best[0]:
            best = (key, combo, production, exchanging, effort)

    reasons: list[dict[str, str]] = []
    if best is None:
        return {
            "schemaVersion": "1",
            "status": "infeasible",
            "assignments": [],
            "businessMetrics": None,
            "totals": None,
            "reasons": [{"code": "NO_FEASIBLE_ALLOCATION", "message": "No allocation keeps every business within capacity and no worse off."}],
            "certificate": None,
        }

    _, combo, production, exchanging, effort = best
    assignments = [
        {"deliverableId": d["id"], "providerId": provider, "minutes": estimates[provider][d["service"]]}
        for d, provider in zip(deliverables, combo, strict=True)
    ]
    metrics = [
        {
            "businessId": bid,
            "baselineMinutes": baseline[bid],
            "productionMinutes": production[bid],
            "coordinationMinutes": effort[bid] - production[bid],
            "effortMinutes": effort[bid],
            "netSavingsMinutes": baseline[bid] - effort[bid],
        }
        for bid in businesses
    ]
    totals = {
        "baselineMinutes": sum(baseline.values()),
        "productionMinutes": sum(production.values()),
        "coordinationMinutes": sum(m["coordinationMinutes"] for m in metrics),
        "effortMinutes": sum(effort.values()),
    }
    totals["grossSavingsMinutes"] = totals["baselineMinutes"] - totals["productionMinutes"]
    totals["netSavingsMinutes"] = totals["baselineMinutes"] - totals["effortMinutes"]

    has_cross = bool(exchanging)
    status = "collaboration" if has_cross and totals["netSavingsMinutes"] > 0 else "no_exchange"
    if status == "no_exchange":
        reasons.append({"code": "NO_BENEFICIAL_EXCHANGE", "message": "No exchange saves time without leaving a business worse off."})

    return {
        "schemaVersion": "1",
        "status": status,
        "assignments": assignments,
        "businessMetrics": metrics,
        "totals": totals,
        "reasons": reasons,
        "certificate": {
            "schemaVersion": "1",
            "scenarioDigest": digest(scenario),
            "allocationDigest": digest(assignments),
            "verification": "valid",
            "assumptions": ["Effort is measured in modeled minutes.", "Service quality is assumed equal across providers."],
            "checks": [{"name": "capacity", "passed": True}, {"name": "no business worse off", "passed": True}],
        },
    }
