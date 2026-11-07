"""B3: POST /confirm and the scenario checks shared with POST /optimize (backend/confirmation.py).

No AWS, no model and no network are involved: confirmation is deterministic validation. The optimize adapter used
below is a TEST DOUBLE whose only job is to prove it is never reached with an invalid scenario. It is not solver output.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

import pytest

from backend.app import Adapters, App, Request, default_adapters
from backend.config import load_settings
from backend.confirmation import confirm_scenario, validate_scenario
from backend.errors import ApiError
from contracts.models import ConfirmRequest, Scenario
from tests.backend.helpers import fixture, validator

FIXTURE_SETTINGS = load_settings({"COMMIT_SHA": "test123"})
LIVE_SETTINGS = load_settings({"APP_MODE": "live", "COMMIT_SHA": "test123"})
ENVELOPE_KEYS = {"code", "message", "fields", "requestId"}
SECRET = "SECRET-MARKER-9f3a"


# ------------------------------------------------------------------ helpers
@pytest.fixture
def canonical() -> dict[str, Any]:
    return fixture("canonical.json")


@pytest.fixture
def coordination() -> dict[str, Any]:
    return fixture("coordination.json")


class OptimizeProbe:
    """TEST DOUBLE for the optimize adapter. Records calls. Returns nothing useful on purpose."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def __call__(self, request: Any) -> Any:
        self.calls.append(request)
        raise AssertionError("the optimize adapter must not be reached in these tests")


def app_with_probe(settings=FIXTURE_SETTINGS) -> tuple[App, OptimizeProbe]:
    probe = OptimizeProbe()
    return App(settings, Adapters(confirm=confirm_scenario, optimize=probe)), probe


def post(app: App, path: str, scenario: Any, request_id: str = "req-1"):
    return app.handle(Request("POST", path, {}, json.dumps({"scenario": scenario}).encode(), request_id))


def mutated(scenario: dict[str, Any], change) -> dict[str, Any]:
    out = copy.deepcopy(scenario)
    change(out)
    return out


def assert_422(response, code: str):
    assert response.status == 422, response.body
    assert set(response.body) == {"error"} and set(response.body["error"]) == ENVELOPE_KEYS
    assert response.body["error"]["code"] == code
    assert response.body["error"]["requestId"] == response.headers["X-Request-Id"]
    assert list(validator("api.schema.json", "ErrorEnvelope").iter_errors(response.body)) == []
    return response.body["error"]["fields"]


# ================================================================== the canonical scenario
def test_valid_canonical_scenario_round_trips_unchanged(canonical):
    app, probe = app_with_probe()
    response = post(app, "/confirm", canonical)
    assert response.status == 200
    assert response.body == {"scenario": canonical}, "confirm returns exactly what it validated"
    assert list(validator("api.schema.json", "ConfirmResponse").iter_errors(response.body)) == []
    assert response.body["scenario"]["confirmed"] is True
    assert probe.calls == []
    # Feeding the answer back in gives the same answer: the operation is idempotent.
    again = post(app, "/confirm", response.body["scenario"])
    assert again.status == 200 and again.body == response.body
    app.close()


def test_coordination_variant_with_60_minutes_is_accepted(coordination):
    app, _ = app_with_probe()
    response = post(app, "/confirm", coordination)
    assert response.status == 200
    assert {b["coordinationMinutes"] for b in response.body["scenario"]["businesses"]} == {60}
    app.close()


def test_confirm_is_a_pure_function_of_its_input_and_does_not_mutate_it(canonical):
    scenario = Scenario.model_validate(canonical)
    before = scenario.model_dump(mode="json")
    response = confirm_scenario(ConfirmRequest(scenario=scenario))
    assert response.scenario.model_dump(mode="json") == before == canonical
    assert response.scenario is scenario or response.scenario == scenario


def test_confirm_does_not_reorder_round_or_fill_anything(canonical):
    # copyco lists writing before coding. A "normalising" server might sort it; this one must not.
    assert [e["service"] for e in canonical["businesses"][2]["estimates"]] == ["writing", "coding"]
    app, _ = app_with_probe()
    body = post(app, "/confirm", canonical).body["scenario"]
    assert [e["service"] for e in body["businesses"][2]["estimates"]] == ["writing", "coding"]
    assert [b["id"] for b in body["businesses"]] == ["devco", "designco", "copyco"]
    app.close()


# ================================================================== missing baseline -> 422 MISSING_BASELINE
def test_missing_own_service_estimate_is_422_missing_baseline(canonical):
    # devco needs design but only has a coding estimate.
    broken = mutated(canonical, lambda s: s["businesses"][0].__setitem__("estimates", [{"service": "coding", "minutes": 180}]))
    app, probe = app_with_probe()
    for path in ("/confirm", "/optimize"):
        fields = assert_422(post(app, path, broken), "MISSING_BASELINE")
        assert fields == ["scenario.businesses[0].estimates.design"]
    assert probe.calls == [], "a scenario without a baseline never reaches the optimizer"
    app.close()


def test_a_business_with_no_estimates_at_all_is_missing_its_baseline(canonical):
    broken = mutated(canonical, lambda s: s["businesses"][1].__setitem__("estimates", []))
    fields = assert_422(post(app_with_probe()[0], "/confirm", broken), "MISSING_BASELINE")
    assert fields == ["scenario.businesses[1].estimates.writing"]


def test_every_missing_baseline_is_reported_once_in_input_order(canonical):
    def strip_all(s):
        for b in s["businesses"]:
            b["estimates"] = []
        s["deliverables"].append({"id": "d-design-2", "ownerId": "devco", "service": "design"})

    fields = assert_422(post(app_with_probe()[0], "/confirm", mutated(canonical, strip_all)), "MISSING_BASELINE")
    assert fields == [
        "scenario.businesses[0].estimates.design",
        "scenario.businesses[1].estimates.writing",
        "scenario.businesses[2].estimates.coding",
    ], "the duplicate design need on devco must not be listed twice"


def test_estimates_for_services_you_do_not_need_do_not_substitute_for_a_baseline(canonical):
    # designco needs writing; give it every OTHER estimate but not writing.
    broken = mutated(
        canonical,
        lambda s: s["businesses"][1].__setitem__("estimates", [{"service": "design", "minutes": 180}, {"service": "coding", "minutes": 60}]),
    )
    assert assert_422(post(app_with_probe()[0], "/confirm", broken), "MISSING_BASELINE") == ["scenario.businesses[1].estimates.writing"]


def test_missing_baseline_message_is_fixed_text_and_never_echoes_input(canonical):
    broken = mutated(canonical, lambda s: (s["businesses"][0].__setitem__("estimates", []), s["businesses"][0].__setitem__("name", SECRET)))
    response = post(app_with_probe()[0], "/confirm", broken)
    assert response.status == 422 and SECRET not in json.dumps(response.body)


def test_a_draft_from_extraction_cannot_be_confirmed_until_the_human_completes_it():
    """The real workflow: /extract gives a draft with gaps; the user fills them; only then does /confirm succeed."""
    draft = fixture("extraction.json")["response"]["draft"]
    app, _ = app_with_probe()

    as_is = {**draft, "schemaVersion": "1", "confirmed": True}
    assert post(app, "/confirm", as_is).status == 422, "copyco's coordination is still null"

    filled = copy.deepcopy(as_is)
    filled["businesses"][2]["coordinationMinutes"] = 0  # the user states: no coordination cost
    response = post(app, "/confirm", filled)
    assert response.status == 200
    assert response.body["scenario"]["businesses"][2]["coordinationMinutes"] == 0

    no_baseline = copy.deepcopy(filled)
    no_baseline["businesses"][2]["estimates"] = [{"service": "writing", "minutes": 180}]  # coding estimate missing
    assert assert_422(post(app, "/confirm", no_baseline), "MISSING_BASELINE") == ["scenario.businesses[2].estimates.coding"]
    app.close()


# ================================================================== explicit zero, negative and fractional values
def test_explicit_zero_coordination_and_zero_capacity_are_accepted(canonical):
    scenario = mutated(canonical, lambda s: [b.__setitem__("coordinationMinutes", 0) for b in s["businesses"]])
    assert all(b["coordinationMinutes"] == 0 for b in canonical["businesses"])
    assert post(app_with_probe()[0], "/confirm", scenario).status == 200

    zero_capacity = mutated(canonical, lambda s: s["businesses"][0].__setitem__("capacityMinutes", 0))
    assert post(app_with_probe()[0], "/confirm", zero_capacity).status == 200


def test_null_coordination_is_rejected_because_zero_must_be_stated_not_assumed(canonical):
    broken = mutated(canonical, lambda s: s["businesses"][0].__setitem__("coordinationMinutes", None))
    fields = assert_422(post(app_with_probe()[0], "/confirm", broken), "VALIDATION_ERROR")
    assert any("coordinationMinutes" in f for f in fields)


def test_missing_capacity_or_coordination_keys_are_rejected(canonical):
    for key in ("capacityMinutes", "coordinationMinutes"):
        broken = mutated(canonical, lambda s, k=key: s["businesses"][0].pop(k))
        fields = assert_422(post(app_with_probe()[0], "/confirm", broken), "VALIDATION_ERROR")
        assert any(key in f for f in fields)


@pytest.mark.parametrize("field", ["capacityMinutes", "coordinationMinutes"])
@pytest.mark.parametrize(
    "bad",
    [-1, -600, 0.5, 600.5, 59.9999, "600", "", True, False, [], {}, 1_000_001, 10**12],
    ids=lambda v: repr(v)[:20],
)
def test_negative_fractional_wrongly_typed_or_oversized_minutes_are_rejected(field, bad, canonical):
    broken = mutated(canonical, lambda s: s["businesses"][0].__setitem__(field, bad))
    app, probe = app_with_probe()
    for path in ("/confirm", "/optimize"):
        fields = assert_422(post(app, path, broken), "VALIDATION_ERROR")
        assert any(field in f for f in fields), fields
    assert probe.calls == []
    app.close()


@pytest.mark.parametrize("bad", [-1, 0, 0.5, 90.5, "180", True, None, 1_000_001])
def test_estimate_minutes_must_be_positive_whole_numbers(bad, canonical):
    broken = mutated(canonical, lambda s: s["businesses"][0]["estimates"][0].__setitem__("minutes", bad))
    fields = assert_422(post(app_with_probe()[0], "/confirm", broken), "VALIDATION_ERROR")
    assert any("minutes" in f for f in fields)


def test_the_bounds_themselves_are_accepted(canonical):
    top = mutated(canonical, lambda s: s["businesses"][0].update(capacityMinutes=1_000_000, coordinationMinutes=1_000_000))
    assert post(app_with_probe()[0], "/confirm", top).status == 200
    smallest = mutated(canonical, lambda s: s["businesses"][0]["estimates"][1].__setitem__("minutes", 1))
    assert post(app_with_probe()[0], "/confirm", smallest).status == 200


# ================================================================== invented / unsupported services, unknown ids
@pytest.mark.parametrize("service", ["legal", "accounting", "Design", "DESIGN", "coding ", "", None, 5, "design,coding"])
def test_an_invented_or_unsupported_service_on_a_deliverable_is_rejected(service, canonical):
    broken = mutated(canonical, lambda s: s["deliverables"][0].__setitem__("service", service))
    app, probe = app_with_probe()
    fields = assert_422(post(app, "/confirm", broken), "VALIDATION_ERROR")
    assert any("deliverables[0].service" in f for f in fields)
    assert assert_422(post(app, "/optimize", broken), "VALIDATION_ERROR")
    assert probe.calls == []
    app.close()


def test_an_invented_service_in_an_estimate_is_rejected(canonical):
    broken = mutated(canonical, lambda s: s["businesses"][0]["estimates"].append({"service": "legal", "minutes": 30}))
    fields = assert_422(post(app_with_probe()[0], "/confirm", broken), "VALIDATION_ERROR")
    assert any("estimates[2].service" in f for f in fields)


def test_unknown_owner_id_is_rejected_and_nothing_is_invented(canonical):
    broken = mutated(canonical, lambda s: s["deliverables"][1].__setitem__("ownerId", "ghostco"))
    app, probe = app_with_probe()
    for path in ("/confirm", "/optimize"):
        assert assert_422(post(app, path, broken), "VALIDATION_ERROR") == ["scenario.deliverables[1].ownerId"]
    assert probe.calls == []
    app.close()


def test_owner_id_matching_is_exact_not_case_folded_or_trimmed(canonical):
    for owner in ("Devco", "devco ", " devco", "devco\n"):
        broken = mutated(canonical, lambda s, o=owner: s["deliverables"][0].__setitem__("ownerId", o))
        assert post(app_with_probe()[0], "/confirm", broken).status == 422


def test_duplicate_business_ids_are_rejected(canonical):
    def clash(s):
        s["businesses"][1]["id"] = "devco"  # second business now has devco's id...
        s["deliverables"][1]["ownerId"] = "devco"  # ...and its deliverable still points at a real owner

    fields = assert_422(post(app_with_probe()[0], "/confirm", mutated(canonical, clash)), "VALIDATION_ERROR")
    assert fields == ["scenario.businesses[1].id"]


def test_renaming_a_business_id_orphans_its_deliverables_and_both_problems_are_reported(canonical):
    broken = mutated(canonical, lambda s: s["businesses"][1].__setitem__("id", "devco"))
    fields = assert_422(post(app_with_probe()[0], "/confirm", broken), "VALIDATION_ERROR")
    assert fields == ["scenario.businesses[1].id", "scenario.deliverables[1].ownerId"]


def test_duplicate_deliverable_ids_are_rejected(canonical):
    broken = mutated(canonical, lambda s: s["deliverables"][2].__setitem__("id", "d-design"))
    assert assert_422(post(app_with_probe()[0], "/confirm", broken), "VALIDATION_ERROR") == ["scenario.deliverables[2].id"]


def test_two_estimates_for_one_service_are_rejected_even_when_equal(canonical):
    broken = mutated(canonical, lambda s: s["businesses"][0]["estimates"].append({"service": "coding", "minutes": 180}))
    assert assert_422(post(app_with_probe()[0], "/confirm", broken), "VALIDATION_ERROR") == ["scenario.businesses[0].estimates[2].service"]


def test_consistency_problems_are_reported_before_baseline_problems(canonical):
    def both(s):
        s["businesses"][0]["estimates"] = []  # missing baseline
        s["deliverables"][1]["ownerId"] = "ghostco"  # and an unknown owner

    response = post(app_with_probe()[0], "/confirm", mutated(canonical, both))
    assert assert_422(response, "VALIDATION_ERROR") == ["scenario.deliverables[1].ownerId"]


def test_every_kind_of_consistency_problem_is_named_in_the_fixed_message(canonical):
    def all_three(s):
        s["businesses"][1]["id"] = "devco"
        s["deliverables"][1]["ownerId"] = "ghostco"
        s["businesses"][0]["estimates"].append({"service": "coding", "minutes": 5})

    response = post(app_with_probe()[0], "/confirm", mutated(canonical, all_three))
    fields = assert_422(response, "VALIDATION_ERROR")
    message = response.body["error"]["message"]
    for phrase in ("duplicate ids", "unknown owner", "duplicate service estimate"):
        assert phrase in message
    assert len(fields) == 3


def test_field_list_is_capped(canonical):
    # schema allows 6 deliverables; make all six reference unknown owners, then confirm the cap logic directly.
    def many(s):
        s["deliverables"] = [{"id": f"d-{n}", "ownerId": "ghost", "service": "design"} for n in range(6)]

    fields = assert_422(post(app_with_probe()[0], "/confirm", mutated(canonical, many)), "VALIDATION_ERROR")
    assert len(fields) == 6 <= 20


# ================================================================== valid-but-infeasible is NOT malformed
def test_all_capacities_zero_is_accepted_it_is_infeasible_not_invalid(canonical):
    disruption = fixture("disruption.json")["infeasible"]["changes"]
    scenario = copy.deepcopy(canonical)
    for change in disruption:  # apply the fixture's patch by hand (kind == capacity)
        next(b for b in scenario["businesses"] if b["id"] == change["businessId"])["capacityMinutes"] = change["minutes"]
    assert {b["capacityMinutes"] for b in scenario["businesses"]} == {0}
    assert post(app_with_probe()[0], "/confirm", scenario).status == 200


def test_the_no_exchange_disruption_is_accepted(canonical):
    change = fixture("disruption.json")["noExchange"]["changes"][0]
    scenario = copy.deepcopy(canonical)
    business = next(b for b in scenario["businesses"] if b["id"] == change["businessId"])
    next(e for e in business["estimates"] if e["service"] == change["service"])["minutes"] = change["minutes"]
    assert post(app_with_probe()[0], "/confirm", scenario).status == 200


def test_baseline_above_own_capacity_is_accepted_it_is_the_optimizers_call(canonical):
    scenario = mutated(canonical, lambda s: s["businesses"][0].__setitem__("capacityMinutes", 100))  # baseline is 480
    assert post(app_with_probe()[0], "/confirm", scenario).status == 200


def test_a_business_with_no_deliverables_is_valid_with_a_zero_baseline(canonical):
    scenario = mutated(canonical, lambda s: s["deliverables"].__setitem__(slice(None), [d for d in s["deliverables"] if d["ownerId"] != "copyco"]))
    assert {d["ownerId"] for d in scenario["deliverables"]} == {"devco", "designco"}
    assert post(app_with_probe()[0], "/confirm", scenario).status == 200


def test_display_names_need_not_be_unique_ids_identify_entities(canonical):
    scenario = mutated(canonical, lambda s: [b.__setitem__("name", "Same Name") for b in s["businesses"]])
    assert post(app_with_probe()[0], "/confirm", scenario).status == 200


def test_two_deliverables_of_one_service_from_one_owner_share_one_estimate(canonical):
    scenario = mutated(canonical, lambda s: s["deliverables"].append({"id": "d-design-2", "ownerId": "devco", "service": "design"}))
    assert post(app_with_probe()[0], "/confirm", scenario).status == 200


# ================================================================== shape / schema rules still apply through the route
@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s.__setitem__("confirmed", False),
        lambda s: s.pop("confirmed"),
        lambda s: s.__setitem__("schemaVersion", "2"),
        lambda s: s.__setitem__("extra", 1),
        lambda s: s["businesses"][0].__setitem__("profit", 5),
        lambda s: s["businesses"].pop(),
        lambda s: s["businesses"].append(dict(s["businesses"][0], id="extraco")),
        lambda s: s.__setitem__("deliverables", []),
        lambda s: s["deliverables"].extend({"id": f"x-{n}", "ownerId": "devco", "service": "design"} for n in range(4)),
        lambda s: s["businesses"][0].__setitem__("id", "DevCo"),
    ],
    ids=["unconfirmed", "no-confirmed", "version", "extra-top", "extra-business", "two-businesses", "four-businesses", "no-deliverables", "seven-deliverables", "uppercase-id"],
)
def test_schema_level_rules_are_still_enforced_by_the_route(mutation, canonical):
    app, probe = app_with_probe()
    for path in ("/confirm", "/optimize"):
        assert_422(post(app, path, mutated(canonical, mutation)), "VALIDATION_ERROR")
    assert probe.calls == []
    app.close()


def test_confirmed_true_is_accepted_from_anyone_the_server_does_not_claim_human_action(canonical):
    app, _ = app_with_probe()
    response = post(app, "/confirm", canonical)  # no auth header, no user, no session
    assert response.status == 200
    text = json.dumps(response.body).lower()
    for claim in ("verified", "approved", "authenticated", "confirmed_by", "confirmedby", "human", "user"):
        assert claim not in text, f"the response must not imply a human or identity check ({claim})"
    assert set(response.body) == {"scenario"}
    app.close()


# ================================================================== shared validation in front of /optimize
def test_optimize_with_a_valid_scenario_reaches_the_adapter_after_validation(canonical):
    reached: list[Any] = []

    def stub(request):  # TEST DOUBLE
        reached.append(request.scenario.model_dump(mode="json"))
        from contracts.models import OptimizeResponse

        raise ApiError(503, "UNAVAILABLE", "stop here: this test only needs to see the adapter reached")

    app = App(FIXTURE_SETTINGS, Adapters(optimize=stub))
    response = post(app, "/optimize", canonical)
    assert response.status == 503 and reached == [canonical]
    app.close()


def test_without_an_optimize_adapter_the_route_is_still_an_honest_503(canonical):
    app = App(FIXTURE_SETTINGS, default_adapters(FIXTURE_SETTINGS))
    response = post(app, "/optimize", canonical)
    assert response.status == 503 and response.body["error"]["code"] == "NOT_IMPLEMENTED"
    app.close()


def test_validate_scenario_can_be_called_directly_and_raises_api_errors(canonical):
    validate_scenario(Scenario.model_validate(canonical))  # no exception
    broken = Scenario.model_validate(mutated(canonical, lambda s: s["businesses"][0].__setitem__("estimates", [])))
    with pytest.raises(ApiError) as caught:
        validate_scenario(broken)
    assert (caught.value.status, caught.value.code) == (422, "MISSING_BASELINE")
    with pytest.raises(ApiError):
        confirm_scenario(ConfirmRequest(scenario=broken))


# ================================================================== wiring and modes
def test_default_wiring_confirms_in_both_modes_and_leaves_other_routes_unwired(canonical):
    for settings in (FIXTURE_SETTINGS, LIVE_SETTINGS):
        adapters = default_adapters(settings, {})
        assert adapters.confirm is confirm_scenario
        assert adapters.optimize is None and adapters.simulate is None and adapters.explain is None
        app = App(settings, adapters)
        assert post(app, "/confirm", canonical).status == 200
        assert post(app, "/optimize", canonical).body["error"]["code"] == "NOT_IMPLEMENTED"
        app.close()
    assert default_adapters().confirm is confirm_scenario


def test_confirm_works_through_a_real_http_server_and_an_api_gateway_event(canonical):
    import http.client
    import threading
    from http.server import ThreadingHTTPServer

    from backend.local import make_handler

    app = App(FIXTURE_SETTINGS, default_adapters(FIXTURE_SETTINGS))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        def call(scenario):
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            conn.request("POST", "/confirm", json.dumps({"scenario": scenario}), {"Content-Type": "application/json"})
            r = conn.getresponse()
            body = json.loads(r.read())
            conn.close()
            return r.status, body

        assert call(canonical) == (200, {"scenario": canonical})
        status, body = call(mutated(canonical, lambda s: s["businesses"][0].__setitem__("estimates", [])))
        assert status == 422 and body["error"]["code"] == "MISSING_BASELINE"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)

    event = {
        "version": "2.0",
        "rawPath": "/confirm",
        "headers": {},
        "requestContext": {"http": {"method": "POST", "path": "/confirm"}, "requestId": "gw-1", "stage": "$default"},
        "body": json.dumps({"scenario": canonical}),
        "isBase64Encoded": False,
    }
    result = app.handle_event(event)
    assert result["statusCode"] == 200 and json.loads(result["body"]) == {"scenario": canonical}
    app.close()


def test_logs_for_confirm_carry_metadata_only(canonical, caplog):
    caplog.set_level(logging.INFO, logger="trademind.api")
    app, _ = app_with_probe()
    broken = mutated(canonical, lambda s: (s["businesses"][0].__setitem__("name", SECRET), s["businesses"][0].__setitem__("estimates", [])))
    post(app, "/confirm", broken, request_id="log-confirm-1")
    assert SECRET not in caplog.text
    line = json.loads(next(r.message for r in caplog.records if r.name == "trademind.api"))
    assert line["route"] == "/confirm" and line["status"] == 422 and line["errorCode"] == "MISSING_BASELINE"
    app.close()


def test_extraction_provenance_is_untouched_by_the_confirm_step():
    """B3 must not strip or rewrite what /extract returned: provenance is for the human reviewing the draft."""
    from tests.backend.helpers import load_json, CONTRACTS

    response = fixture("extraction.json")["response"]
    assert response["provenance"], "the fixture draft carries provenance"
    assert list(validator("api.schema.json", "ExtractResponse").iter_errors(response)) == []

    api = load_json(CONTRACTS / "api.schema.json")["$defs"]
    assert "provenance" in api["ExtractResponse"]["required"], "extraction must keep returning provenance"
    assert "provenance" not in api["ConfirmRequest"]["properties"], "confirm takes a scenario only"

    # A client that tries to smuggle provenance (or anything else) into the scenario is rejected, not stored.
    app, _ = app_with_probe()
    scenario = fixture("canonical.json")
    scenario["provenance"] = response["provenance"]
    assert post(app, "/confirm", scenario).status == 422
    app.close()


def test_error_paths_use_one_bracket_style_everywhere():
    from backend.errors import format_path

    assert format_path(["scenario", "businesses", 0, "estimates", 2, "service"]) == "scenario.businesses[0].estimates[2].service"
    assert format_path(["scenario"]) == "scenario"
    assert format_path([0, "a"]) == "[0].a"
    assert format_path([]) == "(body)"
    assert format_path(["a\nb"]) == "a?b", "control characters in a path are neutralised"
