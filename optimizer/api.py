"""The optimizer's public entry points: plain dicts in, plain dicts out (the seam the backend imports).

    optimizer.api.optimize(scenario: dict) -> dict      # a v1 `Result`
    optimizer.api.simulate(scenario: dict, changes: list[dict]) -> dict
    optimizer.api.verify(scenario: dict, result: dict) -> dict

`backend/optimization.py` imports `optimizer.api.optimize` lazily and checks the answer again with its own independent
arithmetic, so nothing here is trusted blindly, and nothing here talks to AWS, a model, a network or a file.

Errors are raised as `ValueError` subclasses and are translated by the caller:
  * `MissingBaselineError`        -> 422 MISSING_BASELINE   (an owner has no estimate for its own deliverable)
  * `OptimizerInputError`         -> 422 VALIDATION_ERROR   (the dict is not a valid scenario / change list)
  * `optimizer.simulation.SimulationError` -> 422 VALIDATION_ERROR (a what-if change that cannot be applied)

Business count: this entry point is the MVP gate. Exactly three businesses are accepted unless `extended=True` is passed, which
allows up to five (the optional A9 extension). The shared v1 schema allows exactly three, so the backend never passes it.
"""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter, ValidationError

from optimizer.contract import Change, Result, Scenario
from optimizer.errors import MissingBaselineError
from optimizer.models import OptimizerConfig, validate_scenario_bounds
from optimizer.solver import apply_changes, solve
from optimizer.solver import verify as _verify

__all__ = ["MissingBaselineError", "OptimizerInputError", "optimize", "simulate", "verify"]

_CHANGES = TypeAdapter(list[Change])


class OptimizerInputError(ValueError):
    """The input is not a valid scenario, result or change list. `fields` lists only field PATHS, never values."""

    def __init__(self, message: str, fields: list[str] | None = None) -> None:
        super().__init__(message)
        self.fields = fields or []


def _paths(error: ValidationError) -> list[str]:
    paths: list[str] = []
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        path = ""
        for part in item["loc"]:
            path += f"[{part}]" if isinstance(part, int) else (("." if path else "") + str(part))
        if path not in paths:
            paths.append(path or "(body)")
    return paths[:20]


def _scenario(data: Any, extended: bool) -> Scenario:
    try:
        scenario = Scenario.model_validate(data)
    except ValidationError as exc:
        raise OptimizerInputError("The scenario is not valid.", _paths(exc)) from None
    try:
        validate_scenario_bounds(scenario, OptimizerConfig(extended_mode=extended))
    except ValueError as exc:
        raise OptimizerInputError(str(exc), getattr(exc, "fields", [])) from None
    return scenario


def _result_dict(result: Result) -> dict[str, Any]:
    return result.model_dump(mode="json")


def optimize(scenario: dict[str, Any], *, extended: bool = False) -> dict[str, Any]:
    """Exact search over every allocation; returns a v1 Result as a plain dict."""
    parsed = _scenario(scenario, extended)
    return _result_dict(solve(parsed, OptimizerConfig(extended_mode=extended)))


def simulate(scenario: dict[str, Any], changes: list[dict[str, Any]], *, extended: bool = False) -> dict[str, Any]:
    """Apply validated what-if changes to a COPY of the scenario, then re-run the exact search. The input is not modified."""
    parsed = _scenario(scenario, extended)
    try:
        typed = _CHANGES.validate_python(changes)
    except ValidationError as exc:
        raise OptimizerInputError("The changes are not valid.", _paths(exc)) from None
    changed = apply_changes(parsed, typed)
    return _result_dict(solve(changed, OptimizerConfig(extended_mode=extended)))


def verify(scenario: dict[str, Any], result: dict[str, Any], *, extended: bool = False) -> dict[str, Any]:
    """Recompute a client-supplied result from the scenario alone. Raises ValueError if its assignments are not valid."""
    parsed = _scenario(scenario, extended)
    try:
        claimed = Result.model_validate(result)
    except ValidationError as exc:
        raise OptimizerInputError("The result is not valid.", _paths(exc)) from None
    return _result_dict(_verify(parsed, claimed))
