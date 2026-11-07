"""POST /confirm and the scenario checks shared with POST /optimize.

JSON Schema (contracts/*.schema.json) and the generated Pydantic models already enforce shapes: required
fields, no nulls, whole non-negative minutes up to 1,000,000, the frozen service list, exactly 3 businesses.
They cannot express rules that compare parts of the scenario with each other. `validate_scenario` adds those:

  consistency (422 VALIDATION_ERROR)
    * business ids are unique, deliverable ids are unique
    * every deliverable's ownerId is a business in this scenario (no unknown or invented ids)
    * a business has at most one estimate per service
  baseline (422 MISSING_BASELINE)
    * every deliverable's OWNER has an estimate for the service it needs itself. Without it the owner's
      baseline (what it costs to do the work alone) is undefined, and "missing" is never treated as zero.

What this module deliberately does NOT reject: a scenario that is valid but cannot work out, for example every
capacity at 0, or an owner whose baseline exceeds its own capacity. Those are the optimizer's `infeasible`
outcome, not malformed input. A business with no deliverables is valid (baseline 0).

About `confirmed: true`: the schema makes it mandatory, but the server cannot know a human did anything. Any
client can send it. The real control is the UI workflow (the user reviews the extraction draft, fills every
missing number, and presses confirm). This endpoint only checks that what arrives is complete and consistent,
and it never claims otherwise: it has no authentication and its response carries no "confirmed by" statement.

`confirm_scenario` returns exactly what it validated. It does not reorder, round or fill anything in.
"""

from __future__ import annotations

from backend.errors import MAX_REPORTED_FIELDS, ApiError, validation_error
from contracts.models import ConfirmRequest, ConfirmResponse, Scenario

MISSING_BASELINE_MESSAGE = (
    "Each business needs its own time estimate for every service it requests, so its baseline can be computed. "
    "See 'fields' for the missing estimates."
)


def _capped(fields: list[str]) -> list[str]:
    return fields[:MAX_REPORTED_FIELDS]


def validate_scenario(scenario: Scenario) -> None:
    """Raise ApiError(422) if the scenario is internally inconsistent or lacks a baseline. Pure and O(n)."""
    problems: dict[str, list[str]] = {"duplicate ids": [], "unknown owner": [], "duplicate service estimate": []}

    business_ids: set[str] = set()
    for i, business in enumerate(scenario.businesses):
        if business.id.root in business_ids:
            problems["duplicate ids"].append(f"scenario.businesses[{i}].id")
        business_ids.add(business.id.root)
        services: set[str] = set()
        for j, estimate in enumerate(business.estimates):
            if estimate.service.root in services:
                problems["duplicate service estimate"].append(f"scenario.businesses[{i}].estimates[{j}].service")
            services.add(estimate.service.root)

    deliverable_ids: set[str] = set()
    for k, deliverable in enumerate(scenario.deliverables):
        if deliverable.id.root in deliverable_ids:
            problems["duplicate ids"].append(f"scenario.deliverables[{k}].id")
        deliverable_ids.add(deliverable.id.root)
        if deliverable.ownerId.root not in business_ids:
            problems["unknown owner"].append(f"scenario.deliverables[{k}].ownerId")

    found = {kind: paths for kind, paths in problems.items() if paths}
    if found:
        # Fixed wording only: the message never repeats any value from the request.
        raise validation_error(
            "Scenario is inconsistent: " + ", ".join(found) + ". See 'fields' for the offending paths.",
            _capped([path for paths in found.values() for path in paths]),
        )

    offered = [{e.service.root for e in b.estimates} for b in scenario.businesses]
    index_of = {b.id.root: i for i, b in enumerate(scenario.businesses)}
    missing: list[str] = []
    for deliverable in scenario.deliverables:
        i = index_of[deliverable.ownerId.root]
        if deliverable.service.root not in offered[i]:
            path = f"scenario.businesses[{i}].estimates.{deliverable.service.root}"
            if path not in missing:
                missing.append(path)
    if missing:
        raise ApiError(422, "MISSING_BASELINE", MISSING_BASELINE_MESSAGE, _capped(missing))


def confirm_scenario(request: ConfirmRequest) -> ConfirmResponse:
    """Adapter for Adapters.confirm. Validates, then returns the same scenario unchanged."""
    validate_scenario(request.scenario)
    return ConfirmResponse(scenario=request.scenario)
