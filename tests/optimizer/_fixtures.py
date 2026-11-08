"""Fixture access for the optimizer tests.

The frozen shared fixtures (`fixtures/*.json`) are bare scenarios, and `disruption.json` lists CHANGES to apply to the canonical
scenario (it is not a scenario itself). This helper presents them in the shape the optimizer tests use:

    load("canonical.json")      -> {"scenario": {...}}            (the bare scenario, wrapped)
    load("coordination.json")   -> {"scenario": {...}}
    load("disruption.json")     -> {"scenario": {...}, "changes": [...]}   canonical + the fixture's `noExchange` changes applied
    load("infeasible.json")     -> {"scenario": {...}, "changes": [...]}   canonical + the fixture's `infeasible` changes applied

The changes are applied by plain dictionary edits here, NOT by `optimizer.simulation`, so the tests of the what-if engine do not
depend on the code they are checking. Nothing is hard coded: the numbers come from the fixture files.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"


def _read(name: str) -> Any:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _apply(scenario: dict[str, Any], changes: list[dict[str, Any]]) -> dict[str, Any]:
    out = deepcopy(scenario)
    for change in changes:
        business = next(b for b in out["businesses"] if b["id"] == change["businessId"])
        kind = change["kind"]
        if kind == "capacity":
            business["capacityMinutes"] = change["minutes"]
        elif kind == "coordination":
            business["coordinationMinutes"] = change["minutes"]
        elif kind == "estimate":
            for estimate in business["estimates"]:
                if estimate["service"] == change["service"]:
                    estimate["minutes"] = change["minutes"]
                    break
            else:
                business["estimates"].append({"service": change["service"], "minutes": change["minutes"]})
        else:  # pragma: no cover - the fixtures only contain the three kinds
            raise ValueError(kind)
    return out


def load(name: str) -> dict[str, Any]:
    if name in ("canonical.json", "coordination.json"):
        return {"scenario": _read(name)}
    if name in ("disruption.json", "infeasible.json"):
        key = "noExchange" if name == "disruption.json" else "infeasible"
        changes = _read("disruption.json")[key]["changes"]
        return {"scenario": _apply(_read("canonical.json"), changes), "changes": changes}
    return _read(name)
