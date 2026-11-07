"""Schema / Pydantic / fixture parity (Phase 0).

What this proves:
  * every contract schema is a valid JSON Schema (2020-12) and every cross-file $ref resolves;
  * the canonical, coordination, disruption and extraction fixtures validate against the
    schemas AND parse through the generated Pydantic models;
  * invalid inputs are rejected by BOTH the JSON Schema and the Pydantic models
    (the TypeScript side runs the same cases in frontend/src/contracts.test.ts);
  * fixture arithmetic matches the frozen contract (baseline 1440 etc.). That arithmetic is
    computed here from fixture inputs only. It is NOT the optimizer.
  * generated contracts/models.py is up to date with the schemas.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from contracts import models
from tests.backend.helpers import (
    CONTRACTS,
    ROOT,
    SCHEMA_FILES,
    apply_ops,
    fixture,
    load_json,
    replace_canonical,
    validator,
)

CASES = load_json(ROOT / "fixtures" / "contract-cases.json")
SCENARIO_FIXTURES = ["canonical.json", "coordination.json"]


# ---------------------------------------------------------------- schemas themselves
@pytest.mark.parametrize("name", SCHEMA_FILES)
def test_schema_is_valid_draft_2020_12(name):
    Draft202012Validator.check_schema(load_json(CONTRACTS / name))


def test_openapi_parses_and_every_ref_resolves():
    doc = yaml.safe_load((CONTRACTS / "api.openapi.yaml").read_text(encoding="utf-8"))
    assert doc["openapi"].startswith("3.1")
    api_defs = set(load_json(CONTRACTS / "api.schema.json")["$defs"])

    refs: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref":
                    refs.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    assert refs, "expected the OpenAPI document to reference the shared schemas"
    for ref in refs:
        if ref.startswith("api.schema.json#/$defs/"):
            assert ref.rsplit("/", 1)[1] in api_defs, f"dangling ref {ref}"
        else:
            assert ref.startswith("#/components/responses/"), f"unexpected ref {ref}"
    for route in ("/health", "/extract", "/confirm", "/optimize", "/simulate", "/explain"):
        assert route in doc["paths"]


# ---------------------------------------------------------------- valid fixtures
@pytest.mark.parametrize("name", SCENARIO_FIXTURES)
def test_scenario_fixture_validates_in_schema_and_pydantic(name):
    data = fixture(name)
    assert list(validator("scenario.schema.json").iter_errors(data)) == []
    models.Scenario.model_validate(data)


def test_extraction_fixture_validates_in_schema_and_pydantic():
    data = fixture("extraction.json")
    assert list(validator("api.schema.json", "ExtractRequest").iter_errors(data["request"])) == []
    assert list(validator("api.schema.json", "ExtractResponse").iter_errors(data["response"])) == []
    models.ExtractRequest.model_validate(data["request"])
    response = models.ExtractResponse.model_validate(data["response"])
    assert response.mode.root == "fixture", "extraction fixture must be labelled as simulated"


@pytest.mark.parametrize("key", ["noExchange", "infeasible"])
def test_disruption_changes_validate_in_schema_and_pydantic(key):
    changes = fixture("disruption.json")[key]["changes"]
    assert changes
    change_validator = validator("api.schema.json", "Change")
    adapter = TypeAdapter(models.Change)
    for change in changes:
        assert list(change_validator.iter_errors(change)) == []
        adapter.validate_python(change)


# ---------------------------------------------------------------- invalid inputs
@pytest.mark.parametrize("case", CASES["scenarioInvalid"], ids=lambda c: c["name"])
def test_invalid_scenario_rejected_by_schema_and_pydantic(case, canonical):
    data = apply_ops(canonical, case["ops"])
    assert list(validator("scenario.schema.json").iter_errors(data)), "JSON Schema accepted invalid data"
    with pytest.raises(ValidationError):
        models.Scenario.model_validate(data)


@pytest.mark.parametrize("case", CASES["simulateInvalid"], ids=lambda c: c["name"])
def test_invalid_simulate_request_rejected_by_schema_and_pydantic(case, canonical):
    data = replace_canonical(case["request"], canonical)
    assert list(validator("api.schema.json", "SimulateRequest").iter_errors(data)), "JSON Schema accepted invalid data"
    # Pydantic cannot express the 'text => changes=[] and apply=false' rule from the schema's
    # if/then. Those two cases are enforced by the backend simulate validator (B-track).
    if case["name"] in ("text together with changes", "text with apply true (text is proposal-only)"):
        pytest.xfail(
            "conditional rule is not representable in generated Pydantic models; "
            "enforced by App._check_request_rules (B1), tested in tests/backend/test_routes.py"
        )
    with pytest.raises(ValidationError):
        models.SimulateRequest.model_validate(data)


@pytest.mark.parametrize("case", CASES["simulateValid"], ids=lambda c: c["name"])
def test_valid_simulate_request_accepted_by_schema_and_pydantic(case, canonical):
    data = replace_canonical(case["request"], canonical)
    assert list(validator("api.schema.json", "SimulateRequest").iter_errors(data)) == []
    models.SimulateRequest.model_validate(data)


# ---------------------------------------------------------------- result schema shape rules
def _result(status, **overrides):
    base = {
        "schemaVersion": "1",
        "status": status,
        "assignments": [],
        "businessMetrics": None,
        "totals": None,
        "reasons": [],
        "certificate": None,
    }
    base.update(overrides)
    return base


def test_infeasible_result_must_have_null_totals_and_certificate():
    check = validator("result.schema.json")
    assert list(check.iter_errors(_result("infeasible", reasons=[{"code": "CAPACITY", "message": "no valid allocation"}]))) == []
    bad = _result(
        "infeasible",
        totals={
            "baselineMinutes": 1,
            "productionMinutes": 1,
            "coordinationMinutes": 0,
            "effortMinutes": 1,
            "grossSavingsMinutes": 0,
            "netSavingsMinutes": 0,
        },
    )
    assert list(check.iter_errors(bad)), "fabricated totals on an infeasible result must be rejected"


def test_non_infeasible_result_cannot_have_null_totals():
    assert list(validator("result.schema.json").iter_errors(_result("collaboration")))


# ---------------------------------------------------------------- semantic fixture invariants
def _baseline_by_business(scenario):
    estimates = {b["id"]: {e["service"]: e["minutes"] for e in b["estimates"]} for b in scenario["businesses"]}
    totals = {b["id"]: 0 for b in scenario["businesses"]}
    for d in scenario["deliverables"]:
        totals[d["ownerId"]] += estimates[d["ownerId"]][d["service"]]  # KeyError => MISSING_BASELINE
    return totals


@pytest.mark.parametrize("name", SCENARIO_FIXTURES)
def test_fixture_semantic_rules(name):
    scenario = fixture(name)
    business_ids = [b["id"] for b in scenario["businesses"]]
    assert len(business_ids) == len(set(business_ids)), "business ids must be unique"
    deliverable_ids = [d["id"] for d in scenario["deliverables"]]
    assert len(deliverable_ids) == len(set(deliverable_ids)), "deliverable ids must be unique"
    for d in scenario["deliverables"]:
        assert d["ownerId"] in business_ids, "deliverable owner must reference a business"
    for b in scenario["businesses"]:
        services = [e["service"] for e in b["estimates"]]
        assert len(services) == len(set(services)), "one estimate per service per business"
    _baseline_by_business(scenario)  # raises if any owner lacks its own baseline estimate


def test_canonical_baseline_matches_frozen_contract():
    scenario = fixture("canonical.json")
    baselines = _baseline_by_business(scenario)
    assert baselines == {"devco": 480, "designco": 480, "copyco": 480}
    assert sum(baselines.values()) == 1440
    assert {b["capacityMinutes"] for b in scenario["businesses"]} == {600}
    assert {b["coordinationMinutes"] for b in scenario["businesses"]} == {0}
    assert Counter((d["ownerId"], d["service"]) for d in scenario["deliverables"]) == Counter(
        {("devco", "design"): 1, ("designco", "writing"): 1, ("copyco", "coding"): 1}
    )


def test_coordination_fixture_differs_only_in_coordination_minutes():
    canonical = fixture("canonical.json")
    coordination = fixture("coordination.json")
    assert {b["coordinationMinutes"] for b in coordination["businesses"]} == {60}
    for b in coordination["businesses"]:
        b["coordinationMinutes"] = 0
    assert coordination == canonical


def test_extraction_fixture_is_honest_about_missing_data_and_quotes():
    data = fixture("extraction.json")
    texts = " ".join(d["text"] for d in data["request"]["descriptions"])
    for item in data["response"]["provenance"]:
        assert item["quote"] in texts, f"provenance quote not found in the descriptions: {item['quote']!r}"
    draft = data["response"]["draft"]
    assert "confirmed" not in draft, "extraction never confirms"
    nulls = [
        f"businesses[{i}].{field}"
        for i, b in enumerate(draft["businesses"])
        for field in ("capacityMinutes", "coordinationMinutes")
        if b[field] is None
    ]
    assert sorted(nulls) == sorted(data["response"]["missingFields"])


# ---------------------------------------------------------------- generated code freshness
def _load_generator():
    spec = importlib.util.spec_from_file_location("generate_contracts", ROOT / "scripts" / "generate_contracts.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_generated_models_py_is_up_to_date():
    generator = _load_generator()
    with tempfile.TemporaryDirectory() as tmp:
        bundle_path = Path(tmp) / "bundle.schema.json"
        bundle_path.write_text(json.dumps(generator.bundle(), indent=2) + "\n", encoding="utf-8", newline="\n")
        fresh = Path(tmp) / "models.py"
        generator.generate_python(bundle_path, fresh)
        committed = (CONTRACTS / "models.py").read_text(encoding="utf-8").replace("\r\n", "\n")
        assert committed == fresh.read_text(encoding="utf-8").replace("\r\n", "\n"), (
            "contracts/models.py is stale. Run: python scripts/generate_contracts.py"
        )
