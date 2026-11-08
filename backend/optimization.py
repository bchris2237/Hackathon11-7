"""POST /optimize: the stable seam between the HTTP API and Developer 1's optimizer.

THE INTERFACE THIS MODULE EXPECTS (to be confirmed with Developer 1; nothing else in the backend imports optimizer code):

    optimizer.api.optimize(scenario: dict) -> dict        # or a Pydantic model / Mapping shaped like Result

  * input: the scenario's JSON form, already validated (schema + backend/confirmation.py rules);
  * output: the contract's `Result` (contracts/result.schema.json);
  * pure and deterministic: no network, no clock, no randomness, no mutation of the input;
  * raise NotImplementedError if it cannot work. Anything else it raises is treated as a bug (HTTP 500).

Why a seam and not an import: the backend must be testable before the optimizer merges, and a production
deploy must NEVER fall back to a simulated solver. So:
  * `load_optimizer_function()` imports `optimizer.api` lazily. If it does not exist the route answers an
    explicit 503 (NOT_IMPLEMENTED); if it exists but fails to import, 503 OPTIMIZER_UNAVAILABLE;
  * tests inject a function. No fixture or canned optimizer is ever built by production wiring.

What the backend verifies about the optimizer's answer (`check_result_consistency`), independently of the optimizer:
the arithmetic identities of the frozen contract (baseline recomputed from the scenario, production from the
assignments, coordination charged once per business that exchanges, effort = production + coordination,
net = baseline - effort, effort <= capacity, net >= 0, totals are sums, status matches the definition). It does NOT
re-solve, so it cannot tell whether an allocation is optimal; that is the optimizer's own test suite's job.
An answer that fails any identity is never returned: the client gets a generic 500, the log gets a fixed reason code.

`mode` on the response is the application's mode. The computation is real in both modes; the label tells the UI
whether the rest of the system (AWS-backed routes) is simulated or live.
"""

from __future__ import annotations

import importlib
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from backend.errors import ApiError
from contracts.models import OptimizeRequest, OptimizeResponse, Result, Scenario

log = logging.getLogger("trademind.optimization")
log.setLevel(logging.INFO)

OptimizeFn = Callable[[dict[str, Any]], Any]
INTERNAL = ("INTERNAL_ERROR", "Internal server error.")


# --------------------------------------------------------------------------- loading
def load_optimizer_function() -> OptimizeFn | None | Exception:
    """Return the optimizer's function, None if the package is simply absent, or the Exception if it is broken."""
    try:
        module = importlib.import_module("optimizer.api")
    except ModuleNotFoundError as exc:
        if exc.name in ("optimizer", "optimizer.api"):
            return None  # not merged yet
        return exc  # optimizer.api exists but one of ITS imports is missing
    except Exception as exc:  # noqa: BLE001 - a broken module must not crash cold start
        return exc
    function = getattr(module, "optimize", None)
    if not callable(function):
        return AttributeError("optimizer.api has no callable 'optimize'")
    return function


def broken_optimizer_adapter(error: Exception) -> Callable[..., Any]:
    """Adapter used when `optimizer.api` exists but cannot be loaded. Always an explicit 503, never a fallback.

    (When the package is simply absent nothing is wired, so B1's generic 503 NOT_IMPLEMENTED applies.)
    """
    log.error(json.dumps({"event": "optimizer_load_failed", "errorType": type(error).__name__}))

    def broken(*_args: Any) -> Any:
        raise ApiError(503, "OPTIMIZER_UNAVAILABLE", "The optimizer package failed to load. An administrator must check the deployment.")

    return broken


# --------------------------------------------------------------------------- running
def run_optimizer(optimize_fn: OptimizeFn, scenario: Scenario) -> Result:
    """Call the optimizer, validate its output against the contract and check its arithmetic. Never returns a bad Result."""
    payload = scenario.model_dump(mode="json")  # a fresh dict: the optimizer cannot alter our copy
    try:
        raw = optimize_fn(payload)
    except NotImplementedError:
        raise ApiError(503, "NOT_IMPLEMENTED", "The optimizer is not implemented yet.") from None
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.error(json.dumps({"event": "optimizer_raised", "errorType": type(exc).__name__}))
        raise ApiError(500, *INTERNAL) from None

    data = raw.model_dump(mode="json") if isinstance(raw, BaseModel) else raw
    if not isinstance(data, Mapping):
        log.error(json.dumps({"event": "optimizer_bad_output", "reason": "not_an_object"}))
        raise ApiError(500, *INTERNAL)
    try:
        result = Result.model_validate(dict(data))
    except ValidationError as exc:
        log.error(json.dumps({"event": "optimizer_bad_output", "reason": "schema", "errorCount": exc.error_count()}))
        raise ApiError(500, *INTERNAL) from None

    problems = result_problems(scenario.model_dump(mode="json"), result.model_dump(mode="json"))
    if problems:
        log.error(json.dumps({"event": "optimizer_inconsistent_result", "problems": problems}))
        raise ApiError(500, *INTERNAL)
    return result


def make_optimize_adapter(optimize_fn: OptimizeFn, mode: str) -> Callable[[OptimizeRequest], OptimizeResponse]:
    """Adapter for Adapters.optimize. `mode` is the app's mode ('fixture' or 'live')."""

    def optimize(request: OptimizeRequest) -> OptimizeResponse:
        result = run_optimizer(optimize_fn, request.scenario)
        return OptimizeResponse.model_validate({"result": result.model_dump(mode="json"), "mode": mode})

    return optimize


# --------------------------------------------------------------------------- independent arithmetic checks
def result_problems(scenario: dict[str, Any], result: dict[str, Any]) -> list[str]:
    """Return fixed reason codes for every contract identity the result breaks. Empty list = consistent."""
    problems: list[str] = []
    status = result["status"]

    if status == "infeasible":
        if result["assignments"]:
            problems.append("infeasible_has_assignments")
        for key in ("businessMetrics", "totals", "certificate"):
            if result[key] is not None:
                problems.append(f"infeasible_has_{key}")
        return problems

    for key in ("businessMetrics", "totals", "certificate"):
        if result[key] is None:
            problems.append(f"missing_{key}")
    if problems:
        return problems

    businesses = {b["id"]: b for b in scenario["businesses"]}
    estimates = {bid: {e["service"]: e["minutes"] for e in b["estimates"]} for bid, b in businesses.items()}
    deliverables = {d["id"]: d for d in scenario["deliverables"]}

    baseline = dict.fromkeys(businesses, 0)
    for d in deliverables.values():
        baseline[d["ownerId"]] += estimates[d["ownerId"]].get(d["service"], 0)

    assigned = [a["deliverableId"] for a in result["assignments"]]
    if sorted(assigned) != sorted(deliverables):
        problems.append("assignments_do_not_cover_each_deliverable_once")
        return problems

    production = dict.fromkeys(businesses, 0)
    exchanging: set[str] = set()
    has_cross = False
    for a in result["assignments"]:
        d = deliverables[a["deliverableId"]]
        provider = a["providerId"]
        if provider not in businesses:
            problems.append("unknown_provider")
            continue
        offered = estimates[provider].get(d["service"])
        if offered is None:
            problems.append("provider_does_not_offer_service")
        elif offered != a["minutes"]:
            problems.append("assignment_minutes_differ_from_provider_estimate")
        production[provider] += a["minutes"]
        if provider != d["ownerId"]:
            has_cross = True
            exchanging.update((provider, d["ownerId"]))
    if problems:
        return problems

    expected: dict[str, dict[str, int]] = {}
    for bid, b in businesses.items():
        coordination = b["coordinationMinutes"] if bid in exchanging else 0
        effort = production[bid] + coordination
        expected[bid] = {
            "baselineMinutes": baseline[bid],
            "productionMinutes": production[bid],
            "coordinationMinutes": coordination,
            "effortMinutes": effort,
            "netSavingsMinutes": baseline[bid] - effort,
        }
        if effort > b["capacityMinutes"]:
            problems.append("effort_exceeds_capacity")
        if baseline[bid] - effort < 0:
            problems.append("a_business_is_worse_off")

    reported = {m["businessId"]: {k: v for k, v in m.items() if k != "businessId"} for m in result["businessMetrics"]}
    if len(reported) != len(result["businessMetrics"]) or set(reported) != set(businesses):
        problems.append("metrics_do_not_list_each_business_once")
    elif reported != expected:
        problems.append("business_metrics_differ_from_recomputation")

    totals = {key: sum(m[key] for m in expected.values()) for key in ("baselineMinutes", "productionMinutes", "coordinationMinutes", "effortMinutes")}
    totals["grossSavingsMinutes"] = totals["baselineMinutes"] - totals["productionMinutes"]
    totals["netSavingsMinutes"] = totals["baselineMinutes"] - totals["effortMinutes"]
    if result["totals"] != totals:
        problems.append("totals_differ_from_recomputation")

    beneficial = has_cross and totals["netSavingsMinutes"] > 0
    if status == "collaboration" and not beneficial:
        problems.append("collaboration_without_a_strictly_beneficial_exchange")
    if status == "no_exchange" and beneficial:
        problems.append("no_exchange_but_a_beneficial_exchange_is_returned")
    return problems
