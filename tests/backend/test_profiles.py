"""B7: optional profile storage (backend/profiles.py).

SIMULATED: `FakeDynamo` below is a test double for the DynamoDB client. It imitates the behaviours that matter (a conditional put that
fails on an existing key, items that outlive their TTL until "deleted", typed attribute values) and has ONLY put_item and get_item, so if
the code ever tried scan/query/update/delete these tests would crash. It proves our logic. It proves NOTHING about a real table, IAM,
TTL being enabled, or latency. Live verification (scripts/smoke.py --profiles and the aws dynamodb commands in docs/deployment.md)
needs a deployed stack and is recorded in docs/compliance/B7.md as NOT RUN.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import secrets
from typing import Any

import pytest
import yaml
from botocore.exceptions import ClientError, EndpointConnectionError, NoRegionError

from backend.app import Adapters, App, Request, default_adapters
from backend.config import ConfigError, load_settings
from backend.confirmation import confirm_scenario
from backend.errors import ApiError
from backend.profiles import (
    DISABLED_MESSAGE,
    PURPOSE,
    ProfileStore,
    build_profile_adapters,
    canonical_json,
    check_fictional,
    digest_of,
    generate_profile_id,
    id_problem,
    looks_personal,
    storage_key,
    validate_profile_id,
)
from contracts.models import ProfilePutRequest, Scenario
from tests.backend.helpers import ROOT, fixture, validator
from tests.backend.reference_optimizer import reference_optimize
from tests.backend.test_deployment import CfnLoader, TEMPLATE_PATH, full_adapters, run_smoke, served  # noqa: F401  (served is a fixture)

FIXTURE_SETTINGS = load_settings({"COMMIT_SHA": "test123"})
LIVE_SETTINGS = load_settings({"APP_MODE": "live", "COMMIT_SHA": "test123"})
TABLE = "trademind-profiles-test"
SECRET = "SECRET-MARKER-9f3a"
NOW = 1_800_000_000.0
ENVELOPE_KEYS = {"code", "message", "fields", "requestId"}


# ------------------------------------------------------------------ the test double
def client_error(code: str, operation: str = "PutItem") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "arn:aws:dynamodb:us-west-2:123456789012:table/secret"}, "ResponseMetadata": {"RequestId": "ddb-req-1"}}, operation)


class FakeDynamo:
    """TEST DOUBLE for the boto3 DynamoDB client. Only put_item and get_item exist, on purpose."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_put: BaseException | None = None
        self.fail_get: BaseException | None = None

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("put_item", kwargs))
        if self.fail_put is not None:
            raise self.fail_put
        assert kwargs["TableName"] == TABLE
        key = kwargs["Item"]["pk"]["S"]
        assert kwargs["ConditionExpression"] == "attribute_not_exists(pk)", "a write must be conditional: no overwrite is ever allowed"
        if key in self.items:
            raise client_error("ConditionalCheckFailedException")
        self.items[key] = copy.deepcopy(kwargs["Item"])
        return {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_item", kwargs))
        if self.fail_get is not None:
            raise self.fail_get
        assert kwargs["TableName"] == TABLE and kwargs.get("ConsistentRead") is True
        item = self.items.get(kwargs["Key"]["pk"]["S"])
        return {"Item": copy.deepcopy(item)} if item else {}


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def canonical() -> dict[str, Any]:
    return fixture("canonical.json")


@pytest.fixture
def parts():
    dynamo, clock = FakeDynamo(), Clock()
    store = ProfileStore(TABLE, 3600, client=dynamo, clock=clock)
    return store, dynamo, clock


def new_id() -> str:
    return generate_profile_id()


def request_for(scenario: dict[str, Any]) -> ProfilePutRequest:
    return ProfilePutRequest.model_validate({"scenario": scenario})


def app_with(store: ProfileStore | None, settings=FIXTURE_SETTINGS) -> App:
    adapters = Adapters(confirm=confirm_scenario)
    if store is not None:
        adapters = Adapters(confirm=confirm_scenario, put_profile=store.put, get_profile=store.get)
    return App(settings, adapters)


def call(app: App, method: str, profile_id: str, body: Any = None, request_id: str = "r1"):
    raw = b"" if body is None else json.dumps(body).encode()
    return app.handle(Request(method, f"/profiles/{profile_id}", {}, raw, request_id))


def assert_error(response, status, code):
    assert response.status == status, response.body
    assert set(response.body) == {"error"} and set(response.body["error"]) == ENVELOPE_KEYS
    assert response.body["error"]["code"] == code, response.body
    assert list(validator("api.schema.json", "ErrorEnvelope").iter_errors(response.body)) == []
    return response.body["error"]


# ================================================================== the canonical scenario, put then get
def test_canonical_scenario_can_be_stored_and_read_back_exactly(parts, canonical):
    store, dynamo, _ = parts
    app = app_with(store)
    profile_id = new_id()
    put = call(app, "PUT", profile_id, {"scenario": canonical})
    assert put.status == 200 and put.body == {"id": profile_id, "scenario": canonical}
    assert list(validator("api.schema.json", "ProfileResponse").iter_errors(put.body)) == []
    got = call(app, "GET", profile_id)
    assert got.status == 200 and got.body == {"id": profile_id, "scenario": canonical}
    assert [name for name, _ in dynamo.calls] == ["put_item", "get_item"]
    app.close()


def test_the_coordination_variant_round_trips_too(parts):
    store, _, _ = parts
    scenario = fixture("coordination.json")
    app = app_with(store)
    pid = new_id()
    assert call(app, "PUT", pid, {"scenario": scenario}).status == 200
    assert call(app, "GET", pid).body["scenario"] == scenario
    app.close()


def test_stored_item_has_the_ttl_attribute_the_table_expects_and_other_safe_fields(parts, canonical):
    store, dynamo, clock = parts
    pid = new_id()
    store.put(pid, request_for(canonical))
    (item,) = dynamo.items.values()
    assert set(item) == {"pk", "scenario", "digest", "schemaVersion", "purpose", "createdAt", "expiresAt"}
    assert item["expiresAt"] == {"N": str(int(NOW) + 3600)}, "TTL attribute: epoch SECONDS as a Number, as DynamoDB requires"
    assert item["purpose"] == {"S": PURPOSE} == {"S": "fictional-demo"}
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", item["createdAt"]["S"])
    assert item["digest"]["S"] == digest_of(item["scenario"]["S"])
    assert json.loads(item["scenario"]["S"]) == canonical


def test_the_record_key_is_a_hash_of_the_id_never_the_id_itself(parts, canonical):
    store, dynamo, _ = parts
    pid = new_id()
    store.put(pid, request_for(canonical))
    (key,) = dynamo.items
    assert key == storage_key(pid) and re.fullmatch(r"[0-9a-f]{64}", key)
    assert pid not in json.dumps(dynamo.items) and pid not in json.dumps([c[1] for c in dynamo.calls], default=str)


def test_only_put_item_and_get_item_are_ever_used(parts, canonical):
    store, dynamo, _ = parts
    pid = new_id()
    store.put(pid, request_for(canonical))
    store.get(pid)
    with pytest.raises(ApiError):
        store.put(pid, request_for(canonical))
    assert {name for name, _ in dynamo.calls} == {"put_item", "get_item"}
    assert not hasattr(dynamo, "scan") and not hasattr(dynamo, "query") and not hasattr(dynamo, "delete_item") and not hasattr(dynamo, "update_item")


# ================================================================== no overwrite: 409 on a duplicate
def test_second_put_to_the_same_id_is_409_and_the_first_record_is_unchanged(parts, canonical):
    store, dynamo, _ = parts
    app = app_with(store)
    pid = new_id()
    assert call(app, "PUT", pid, {"scenario": canonical}).status == 200
    other = fixture("coordination.json")
    error = assert_error(call(app, "PUT", pid, {"scenario": other}), 409, "PROFILE_EXISTS")
    assert "never overwritten" in error["message"]
    assert call(app, "GET", pid).body["scenario"] == canonical, "the original is untouched"
    assert len(dynamo.items) == 1
    app.close()


def test_the_conditional_failure_is_mapped_without_leaking_aws_details(parts, canonical):
    store, dynamo, _ = parts
    dynamo.fail_put = client_error("ConditionalCheckFailedException")
    with pytest.raises(ApiError) as caught:
        store.put(new_id(), request_for(canonical))
    assert (caught.value.status, caught.value.code) == (409, "PROFILE_EXISTS")
    assert "arn:aws" not in caught.value.message and "123456789012" not in caught.value.message


# ================================================================== malformed and weak ids
GOOD_LOOKING = [secrets.token_hex(16), secrets.token_urlsafe(24), secrets.token_urlsafe(80)]


@pytest.mark.parametrize("pid", GOOD_LOOKING)
def test_random_ids_are_accepted(pid):
    assert id_problem(pid) is None
    validate_profile_id(pid)


def test_a_thousand_random_ids_are_never_rejected():
    rejected = [p for p in (generate_profile_id() for _ in range(1000)) if id_problem(p)]
    rejected += [p for p in (secrets.token_hex(16) for _ in range(1000)) if id_problem(p)]
    assert len(rejected) <= 2, f"too many false rejects of random ids: {len(rejected)}"  # guessable-word list can hit ~0.03% of ids


@pytest.mark.parametrize(
    ("pid", "reason"),
    [
        ("a" * 40, "too_few_distinct_characters"),
        ("a" * 21 + "Kq7Zx9Pm2Wv", "low_entropy"),  # 12 distinct characters, so only the entropy floor can refuse it
        ("abc123", "format"),
        ("a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p", "format"),  # 31 characters
        ("x" * 129, "format"),
        ("abcdefghijklmnopqrstuvwxyz012345", "sequence"),
        ("0123456789" * 4, "sequence"),
        ("ab" * 20, "too_few_distinct_characters"),
        ("demo-profile-demo-profile-demo-pr", "guessable_word"),
        ("123e4567-e89b-12d3-a456-426614174000", "guessable_word"),
        ("Trademind-Canonical-Scenario-Number-1", "guessable_word"),
        ("a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6!", "format"),
        ("a1b2c3d4 e5f6g7h8i9j0k1l2m3n4o5p6", "format"),
        ("..................................", "format"),
        ("", "format"),
        ("é" * 40, "format"),
    ],
)
def test_weak_or_malformed_ids_are_refused_with_the_reason_never_the_id(pid, reason):
    assert id_problem(pid) == reason
    with pytest.raises(ApiError) as caught:
        validate_profile_id(pid)
    assert caught.value.status == 422 and caught.value.fields == ["id"]
    assert pid not in caught.value.message or pid == ""


def test_a_scenario_that_would_fail_confirm_is_never_stored(parts, canonical):
    store, dynamo, _ = parts
    app = app_with(store)
    missing = copy.deepcopy(canonical)
    missing["businesses"][0]["estimates"] = []  # schema-valid, but /confirm refuses it: MISSING_BASELINE
    assert_error(call(app, "PUT", new_id(), {"scenario": missing}), 422, "MISSING_BASELINE")
    assert dynamo.items == {} and dynamo.calls == []
    app.close()
    # The store enforces the same rules itself, so a caller that bypasses the App layer still cannot store such a scenario.
    with pytest.raises(ApiError) as caught:
        store.put(new_id(), request_for(missing))
    assert caught.value.status == 422 and caught.value.code == "MISSING_BASELINE"
    assert dynamo.items == {} and dynamo.calls == []


def test_a_weak_id_through_the_http_api_is_422_and_nothing_is_stored(parts, canonical):
    store, dynamo, _ = parts
    app = app_with(store)
    for weak in ("a" * 40, "demo-profile-demo-profile-demo-pr", "abcdefghijklmnopqrstuvwxyz012345"):
        assert assert_error(call(app, "PUT", weak, {"scenario": canonical}), 422, "VALIDATION_ERROR")["fields"] == ["id"]
        assert assert_error(call(app, "GET", weak), 422, "VALIDATION_ERROR")["fields"] == ["id"]
    assert dynamo.calls == [] and dynamo.items == {}
    app.close()


@pytest.mark.parametrize("path_id", ["", "a/b", "has space", "x" * 129, "dot.dot", "semi;colon", "%2e%2e", "../x", "é" * 40])
def test_ids_outside_the_route_pattern_never_reach_the_store(path_id, parts, canonical):
    store, dynamo, _ = parts
    app = app_with(store)
    response = app.handle(Request("PUT", f"/profiles/{path_id}", {}, json.dumps({"scenario": canonical}).encode(), "r"))
    assert response.status in (404, 405) and dynamo.calls == []
    app.close()


# ================================================================== read misses, expiry, TTL lag
def test_reading_an_unknown_id_is_404_without_saying_why(parts):
    store, _, _ = parts
    app = app_with(store)
    error = assert_error(call(app, "GET", new_id()), 404, "PROFILE_NOT_FOUND")
    assert "may have expired" in error["message"]
    app.close()


def test_an_expired_item_that_dynamodb_has_not_deleted_yet_is_treated_as_not_found(parts, canonical):
    store, dynamo, clock = parts
    pid = new_id()
    store.put(pid, request_for(canonical))
    clock.now = NOW + 3599
    assert store.get(pid).id == pid, "still valid one second before expiry"
    clock.now = NOW + 3600
    with pytest.raises(ApiError) as caught:
        store.get(pid)
    assert (caught.value.status, caught.value.code) == (404, "PROFILE_NOT_FOUND")
    assert len(dynamo.items) == 1, "the item is still physically there (TTL deletion is eventual), yet the API will not return it"


def test_unknown_and_expired_are_indistinguishable_to_a_caller(parts, canonical):
    store, _, clock = parts
    pid = new_id()
    store.put(pid, request_for(canonical))
    clock.now = NOW + 99999
    with pytest.raises(ApiError) as expired:
        store.get(pid)
    with pytest.raises(ApiError) as unknown:
        store.get(new_id())
    assert (expired.value.status, expired.value.code, expired.value.message) == (unknown.value.status, unknown.value.code, unknown.value.message)


def test_a_duplicate_is_still_refused_while_an_expired_item_awaits_deletion(parts, canonical):
    # Documented consequence of 'no overwrite': the id stays taken until DynamoDB actually deletes the item (up to 48 h late).
    store, _, clock = parts
    pid = new_id()
    store.put(pid, request_for(canonical))
    clock.now = NOW + 99999
    with pytest.raises(ApiError) as caught:
        store.put(pid, request_for(canonical))
    assert caught.value.status == 409


# ================================================================== damaged or tampered records are never returned
@pytest.mark.parametrize(
    "damage",
    [
        lambda item: item["scenario"].__setitem__("S", item["scenario"]["S"].replace("600", "601", 1)),
        lambda item: item["digest"].__setitem__("S", "0" * 64),
        lambda item: item.pop("digest"),
        lambda item: item.pop("scenario"),
        lambda item: item.pop("expiresAt"),
        lambda item: item["expiresAt"].__setitem__("N", "not-a-number"),
        lambda item: item["scenario"].__setitem__("S", "{not json"),
    ],
    ids=["edited-scenario", "wrong-digest", "no-digest", "no-scenario", "no-expiry", "bad-expiry", "bad-json"],
)
def test_a_damaged_stored_record_is_a_generic_500_and_is_never_returned(damage, parts, canonical, caplog):
    store, dynamo, _ = parts
    caplog.set_level(logging.INFO)
    pid = new_id()
    store.put(pid, request_for(canonical))
    damage(next(iter(dynamo.items.values())))
    with pytest.raises(ApiError) as caught:
        store.get(pid)
    assert (caught.value.status, caught.value.code) == (500, "INTERNAL_ERROR")
    assert pid not in caplog.text and "601" not in caught.value.message


def test_a_stored_scenario_that_no_longer_passes_the_confirm_rules_is_not_returned(parts, canonical):
    store, dynamo, _ = parts
    pid = new_id()
    store.put(pid, request_for(canonical))
    item = next(iter(dynamo.items.values()))
    broken = json.loads(item["scenario"]["S"])
    broken["businesses"][0]["estimates"] = []
    text = canonical_json(broken)
    item["scenario"]["S"], item["digest"]["S"] = text, digest_of(text)  # a consistent digest: only the rules can catch it
    with pytest.raises(ApiError) as caught:
        store.get(pid)
    assert caught.value.status == 500


# ================================================================== strict validation of what is stored
def test_invalid_scenarios_are_never_stored(parts, canonical):
    store, dynamo, _ = parts
    app = app_with(store)
    bad = []
    no_baseline = copy.deepcopy(canonical); no_baseline["businesses"][0]["estimates"] = []; bad.append(no_baseline)
    ghost = copy.deepcopy(canonical); ghost["deliverables"][0]["ownerId"] = "ghostco"; bad.append(ghost)
    fractional = copy.deepcopy(canonical); fractional["businesses"][0]["capacityMinutes"] = 600.5; bad.append(fractional)
    unconfirmed = copy.deepcopy(canonical); unconfirmed["confirmed"] = False; bad.append(unconfirmed)
    extra = copy.deepcopy(canonical); extra["extra"] = 1; bad.append(extra)
    for scenario in bad:
        assert call(app, "PUT", new_id(), {"scenario": scenario}).status == 422
    assert dynamo.calls == []
    app.close()


def test_unknown_fields_and_oversized_bodies_are_rejected_before_storage(parts, canonical):
    store, dynamo, _ = parts
    app = app_with(store)
    assert call(app, "PUT", new_id(), {"scenario": canonical, "ttl": 99999999}).status == 422
    assert call(app, "PUT", new_id(), {"scenario": canonical, "id": "x"}).status == 422
    huge = app.handle(Request("PUT", f"/profiles/{new_id()}", {}, b"x" * (33 * 1024), "r"))
    assert huge.status == 413
    assert call(app, "PUT", new_id(), None).status == 400
    assert dynamo.calls == []
    app.close()


@pytest.mark.parametrize("name", ["jane.doe@example.com", "visit www.example.com", "https://example.test", "Call 555-123-4567", "+1 (555) 123 4567", "Tax id 123 45 6789"])
def test_names_that_look_like_personal_data_are_refused(name, parts, canonical):
    store, dynamo, _ = parts
    scenario = copy.deepcopy(canonical)
    scenario["businesses"][1]["name"] = name
    app = app_with(store)
    error = assert_error(call(app, "PUT", new_id(), {"scenario": scenario}), 422, "PERSONAL_DATA_SUSPECTED")
    assert error["fields"] == ["scenario.businesses[1].name"]
    assert name not in json.dumps(error), "the suspicious text is not echoed"
    assert dynamo.calls == []
    app.close()


@pytest.mark.parametrize("name", ["DevCo", "Studio 77", "Agency 2024-25", "Acme & Sons", "Café Nord", "A" * 200])
def test_ordinary_fictional_names_are_accepted(name, parts, canonical):
    store, _, _ = parts
    scenario = copy.deepcopy(canonical)
    scenario["businesses"][0]["name"] = name
    assert store.put(new_id(), request_for(scenario)).scenario.businesses[0].name == name


def test_the_personal_data_guard_is_best_effort_and_says_so():
    assert looks_personal("a@b.co") and not looks_personal("Plain Name")
    import backend.profiles as profiles_module

    assert "NOT a guarantee" in profiles_module.__doc__
    check_fictional(Scenario.model_validate(fixture("canonical.json")))


# ================================================================== AWS failures: visible errors, never a pretend success
@pytest.mark.parametrize(
    ("raised", "status", "code"),
    [
        (client_error("ProvisionedThroughputExceededException"), 429, "THROTTLED"),
        (client_error("ThrottlingException"), 429, "THROTTLED"),
        (client_error("RequestLimitExceeded"), 429, "THROTTLED"),
        (client_error("AccessDeniedException"), 503, "UNAVAILABLE"),
        (client_error("UnrecognizedClientException"), 503, "UNAVAILABLE"),
        (client_error("ExpiredTokenException"), 503, "UNAVAILABLE"),
        (client_error("ResourceNotFoundException"), 503, "UNAVAILABLE"),
        (client_error("InternalServerError"), 503, "UNAVAILABLE"),
        (client_error("ServiceUnavailable"), 503, "UNAVAILABLE"),
        (client_error("SomethingNewException"), 503, "UNAVAILABLE"),
        (EndpointConnectionError(endpoint_url="https://dynamodb.example"), 503, "UNAVAILABLE"),
    ],
)
def test_dynamodb_failures_map_to_contract_statuses_for_both_routes(raised, status, code, parts, canonical):
    store, dynamo, _ = parts
    pid = new_id()
    dynamo.fail_put = raised
    with pytest.raises(ApiError) as put_error:
        store.put(pid, request_for(canonical))
    dynamo.fail_get = raised
    with pytest.raises(ApiError) as get_error:
        store.get(pid)
    for caught in (put_error, get_error):
        assert (caught.value.status, caught.value.code) == (status, code)
        message = caught.value.message
        assert "arn:aws" not in message and "123456789012" not in message and "dynamodb.example" not in message and TABLE not in message
    assert dynamo.items == {}, "a failed write stores nothing"


def test_a_failed_write_is_not_reported_as_success(parts, canonical):
    store, dynamo, _ = parts
    dynamo.fail_put = client_error("AccessDeniedException")
    app = app_with(store)
    response = call(app, "PUT", new_id(), {"scenario": canonical})
    assert response.status == 503 and "scenario" not in response.body and "id" not in response.body
    app.close()


def test_the_client_is_created_lazily_and_a_missing_region_is_a_clean_503(monkeypatch, canonical):
    import backend.profiles as module

    def no_region(*args, **kwargs):
        raise NoRegionError()

    monkeypatch.setattr(module.boto3, "client", no_region)
    store = ProfileStore(TABLE, 3600)  # construction does not touch AWS
    with pytest.raises(ApiError) as caught:
        store.put(new_id(), request_for(canonical))
    assert caught.value.status == 503 and "region" in caught.value.message


def test_the_real_boto3_client_is_built_with_short_timeouts_and_bounded_retries():
    store = ProfileStore(TABLE, 3600, region="us-west-2")
    client = store._dynamo()  # builds a real client; no network call is made
    cfg = client.meta.config
    assert (cfg.connect_timeout, cfg.read_timeout) == (2, 3) and cfg.retries == {"total_max_attempts": 2, "mode": "standard"}
    assert client.meta.region_name == "us-west-2"


# ================================================================== logs and secrets
def test_logs_never_contain_ids_names_or_scenario_content(parts, canonical, caplog):
    store, dynamo, _ = parts
    caplog.set_level(logging.INFO)
    scenario = copy.deepcopy(canonical)
    scenario["businesses"][0]["name"] = f"Acme {SECRET}"
    pid = new_id()
    app = app_with(store)
    call(app, "PUT", pid, {"scenario": scenario})
    call(app, "PUT", pid, {"scenario": scenario})  # duplicate
    call(app, "GET", pid)
    dynamo.fail_get = client_error("AccessDeniedException")
    call(app, "GET", pid)
    assert pid not in caplog.text and SECRET not in caplog.text and storage_key(pid) not in caplog.text
    events = {json.loads(r.message).get("event") for r in caplog.records if r.name == "trademind.profiles"}
    assert {"profile_stored", "profile_exists", "profile_get_failed"} <= events
    app.close()


def test_routes_log_the_template_not_the_id(parts, canonical, caplog):
    store, _, _ = parts
    caplog.set_level(logging.INFO)
    pid = new_id()
    app = app_with(store)
    call(app, "PUT", pid, {"scenario": canonical})
    assert pid not in caplog.text and "/profiles/{id}" in caplog.text
    app.close()


# ================================================================== disabled by default; the core workflow does not need it
def test_without_a_table_both_routes_say_persistence_is_disabled():
    put, get = build_profile_adapters(None, 3600)
    app = App(FIXTURE_SETTINGS, Adapters(put_profile=put, get_profile=get))
    for method in ("PUT", "GET"):
        body = {"scenario": fixture("canonical.json")} if method == "PUT" else None
        error = assert_error(call(app, method, new_id(), body), 503, "PERSISTENCE_DISABLED")
        assert error["message"] == DISABLED_MESSAGE
    app.close()


def test_default_wiring_is_disabled_unless_a_table_is_configured(canonical):
    for settings in (FIXTURE_SETTINGS, LIVE_SETTINGS):
        adapters = default_adapters(settings, {}, optimizer_loader=lambda: reference_optimize)
        app = App(settings, adapters)
        assert_error(call(app, "PUT", new_id(), {"scenario": canonical}), 503, "PERSISTENCE_DISABLED")
        assert_error(call(app, "GET", new_id()), 503, "PERSISTENCE_DISABLED")
        app.close()


def test_the_core_workflow_works_with_persistence_disabled(canonical):
    settings = LIVE_SETTINGS
    app = App(settings, default_adapters(settings, {}, optimizer_loader=lambda: reference_optimize))
    confirmed = app.handle(Request("POST", "/confirm", {}, json.dumps({"scenario": canonical}).encode(), "c"))
    assert confirmed.status == 200
    optimized = app.handle(Request("POST", "/optimize", {}, json.dumps({"scenario": canonical}).encode(), "o"))
    assert optimized.status == 200 and optimized.body["result"]["totals"]["netSavingsMinutes"] == 900
    simulated = app.handle(Request("POST", "/simulate", {}, json.dumps({"scenario": canonical, "changes": [{"kind": "capacity", "businessId": "devco", "minutes": 600}], "apply": True}).encode(), "s"))
    assert simulated.status == 200
    assert call(app, "GET", new_id()).status == 503, "and the optional routes stay honest"
    app.close()


def test_configured_table_wires_a_real_store_in_both_modes():
    for settings_env in ({"PROFILE_TABLE": TABLE, "PROFILE_TTL_SECONDS": "600", "AWS_REGION": "us-west-2"},):
        for mode in ({}, {"APP_MODE": "live"}):
            settings = load_settings({**settings_env, **mode})
            assert settings.profile_table == TABLE and settings.profile_ttl_seconds == 600
            adapters = default_adapters(settings, {"AWS_REGION": "us-west-2"}, optimizer_loader=lambda: None)
            assert adapters.put_profile.__self__.__class__ is ProfileStore and adapters.get_profile.__self__.__class__ is ProfileStore


def test_a_fake_in_memory_store_is_never_built_by_production_wiring():
    import backend.app as app_module
    import backend.profiles as profiles_module

    source = open(profiles_module.__file__, encoding="utf-8").read() + open(app_module.__file__, encoding="utf-8").read()
    assert "InMemory" not in source and "FakeDynamo" not in source and "defaultdict" not in source


@pytest.mark.parametrize(
    "env",
    [{"PROFILE_TABLE": "x"}, {"PROFILE_TABLE": "has space"}, {"PROFILE_TABLE": "a" * 256}, {"PROFILE_TTL_SECONDS": "10"}, {"PROFILE_TTL_SECONDS": "999999999"}, {"PROFILE_TTL_SECONDS": "soon"}],
)
def test_bad_profile_configuration_is_rejected(env):
    with pytest.raises(ConfigError):
        load_settings(env)


def test_blank_table_means_disabled_and_the_default_ttl_is_24_hours():
    settings = load_settings({"PROFILE_TABLE": "  "})
    assert settings.profile_table is None and settings.profile_ttl_seconds == 86400


# ================================================================== the template: only when enabled, narrowest permissions
@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    return yaml.load(TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=CfnLoader)


def test_table_policy_and_output_exist_only_when_profiles_are_enabled(template):
    assert template["Parameters"]["EnableProfiles"]["Default"] == "false", "off by default"
    assert template["Parameters"]["EnableProfiles"]["AllowedValues"] == ["false", "true"]
    for name in ("ProfilesTable", "ProfilesAccess"):
        assert template["Resources"][name]["Condition"] == "ProfilesEnabled"
    assert template["Outputs"]["ProfilesTableName"]["Condition"] == "ProfilesEnabled"
    assert template["Conditions"]["ProfilesEnabled"] == {"Fn::Equals": [{"Ref": "EnableProfiles"}, "true"]}
    env = template["Resources"]["ApiFunction"]["Properties"]["Environment"]["Variables"]
    assert env["PROFILE_TABLE"] == {"Fn::If": ["ProfilesEnabled", {"Ref": "ProfilesTable"}, {"Ref": "AWS::NoValue"}]}


def test_table_is_on_demand_with_ttl_and_nothing_expensive(template):
    props = template["Resources"]["ProfilesTable"]["Properties"]
    assert props["BillingMode"] == "PAY_PER_REQUEST", "no provisioned capacity to size, pay for or forget"
    assert props["TimeToLiveSpecification"] == {"AttributeName": "expiresAt", "Enabled": True}
    assert props["KeySchema"] == [{"AttributeName": "pk", "KeyType": "HASH"}] and "GlobalSecondaryIndexes" not in props
    assert "ProvisionedThroughput" not in props and "StreamSpecification" not in props
    assert props.get("PointInTimeRecoverySpecification", {}).get("PointInTimeRecoveryEnabled", False) is False
    assert template["Resources"]["ProfilesTable"]["DeletionPolicy"] == "Delete", "disposable demo data goes with the stack"


def test_the_ttl_attribute_in_the_template_is_the_one_the_code_writes(template, parts, canonical):
    store, dynamo, _ = parts
    store.put(new_id(), request_for(canonical))
    (item,) = dynamo.items.values()
    ttl_attribute = template["Resources"]["ProfilesTable"]["Properties"]["TimeToLiveSpecification"]["AttributeName"]
    key_attribute = template["Resources"]["ProfilesTable"]["Properties"]["KeySchema"][0]["AttributeName"]
    assert ttl_attribute in item and "N" in item[ttl_attribute], "TTL needs a Number of epoch seconds"
    assert key_attribute in item and "S" in item[key_attribute]


def test_iam_is_exactly_put_and_get_on_exactly_the_one_table(template):
    policy = template["Resources"]["ProfilesAccess"]["Properties"]
    assert policy["Roles"] == [{"Ref": "ExecutionRole"}]
    (statement,) = policy["PolicyDocument"]["Statement"]
    assert statement["Effect"] == "Allow"
    assert sorted(statement["Action"]) == ["dynamodb:GetItem", "dynamodb:PutItem"]
    assert statement["Resource"] == {"Fn::GetAtt": "ProfilesTable.Arn"}, "the table ARN only: no index, no wildcard"
    assert "Condition" not in statement
    raw = TEMPLATE_PATH.read_text(encoding="utf-8")
    for forbidden in ("dynamodb:*", "dynamodb:Scan", "dynamodb:Query", "dynamodb:DeleteItem", "dynamodb:UpdateItem", "dynamodb:BatchWriteItem", "dynamodb:CreateTable"):
        assert forbidden not in raw


def test_the_base_role_is_unchanged_by_profiles(template):
    role = template["Resources"]["ExecutionRole"]["Properties"]
    assert len(role["Policies"]) == 2 and "ManagedPolicyArns" not in role
    assert "dynamodb" not in json.dumps(role)


def test_ttl_parameter_matches_the_codes_accepted_range(template):
    param = template["Parameters"]["ProfileTtlSeconds"]
    settings_low = load_settings({"PROFILE_TTL_SECONDS": str(param["MinValue"])})
    settings_high = load_settings({"PROFILE_TTL_SECONDS": str(param["MaxValue"])})
    assert settings_low.profile_ttl_seconds == 300 and settings_high.profile_ttl_seconds == 604800
    assert param["Default"] == 86400
    with pytest.raises(ConfigError):
        load_settings({"PROFILE_TTL_SECONDS": str(param["MinValue"] - 1)})
    with pytest.raises(ConfigError):
        load_settings({"PROFILE_TTL_SECONDS": str(param["MaxValue"] + 1)})


# ================================================================== the live smoke steps, against a local server with the fake table (SIMULATED)
def profile_adapters(sdk, settings):
    base = full_adapters(sdk, settings)
    store = ProfileStore(TABLE, 3600, client=FakeDynamo(), clock=Clock())
    return Adapters(
        confirm=base.confirm, optimize=base.optimize, extract=base.extract, explain=base.explain, simulate=base.simulate,
        put_profile=store.put, get_profile=store.get,
    )


def test_smoke_profile_steps_pass_against_a_local_server_with_the_fake_table_SIMULATED(served, capsys):
    base, _ = served(profile_adapters)
    code, out = run_smoke(capsys, "--api-url", base, "--expect-commit", "test123", "--origin", "http://localhost:5173", "--profiles")
    assert "PASS     profile PUT stores the fictional canonical scenario" in out, out
    assert "PASS     profile GET returns exactly what was stored" in out
    assert "PASS     a second PUT to the same id is refused with 409" in out
    assert "PASS     an unknown id is 404" in out and "PASS     a weak id is refused with 422" in out
    assert "PASS     profile route is not a listing endpoint" in out
    assert "FAIL" not in out


def test_smoke_reports_a_failure_when_persistence_is_disabled_but_was_expected(served, capsys):
    base, _ = served(full_adapters)  # profiles unwired: 503 NOT_IMPLEMENTED
    code, out = run_smoke(capsys, "--api-url", base, "--expect-commit", "test123", "--origin", "http://localhost:5173", "--profiles", "--skip-model")
    assert code == 1 and "FAIL     profile PUT stores the fictional canonical scenario" in out


def test_smoke_without_the_profiles_flag_does_not_touch_the_optional_routes(served, capsys):
    base, sdk = served(profile_adapters)
    _, out = run_smoke(capsys, "--api-url", base, "--expect-commit", "test123", "--origin", "http://localhost:5173", "--skip-model")
    assert "profile PUT" not in out and "optional; pass --profiles" in out


# ================================================================== the written guidance
def test_deployment_guide_documents_profiles_honestly():
    text = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    for needle in ("EnableProfiles", "-EnableProfiles", "describe-time-to-live", "describe-table", "fictional", "not authentication", "48 hours", "deletes the table"):
        assert needle in text, f"docs/deployment.md should mention: {needle}"
