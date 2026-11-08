"""PUT /profiles/{id} and GET /profiles/{id}: OPTIONAL storage of one fictional demo scenario in DynamoDB (B7).

READ THIS BEFORE CHANGING ANYTHING: this API has NO authentication. So:
  * Only FICTIONAL demo scenarios belong here. Never personal, customer or confidential data. The server cannot know whether
    data is fictional; `looks_personal` is a best-effort guard against accidents (emails, URLs, phone-like numbers in names), NOT a guarantee.
  * An unguessable id is a SHARED SECRET, not authentication. Anyone who learns an id can read that record until it expires. There
    is no user, no owner, no listing endpoint and no delete endpoint.
  * Records are IMMUTABLE. The first write wins (a DynamoDB conditional put); any later PUT to the same id is 409. There is no overwrite
    because there is no owner who could be allowed to overwrite.
  * Records expire. DynamoDB TTL removes them eventually (it can take up to 48 hours after expiry, per AWS), so GET also checks the
    expiry time itself and treats an expired record as not found.

Design choices:
  * The client picks the id (the route is PUT /profiles/{id}). The server enforces STRENGTH (`validate_profile_id`: length, character set,
    distinct characters, entropy, no runs or repeats, a short list of guessable words). A weak id is 422 and nothing is stored. Strength
    checks cannot prove randomness or detect an arbitrary passphrase; generate ids with a CSPRNG (`generate_profile_id`, or WebCrypto in the browser).
  * The DynamoDB key is SHA-256 of the id, never the id. Anyone who can read the table (or its exports) therefore cannot reuse a key as an id.
  * The stored scenario is re-validated on read (schema, the /confirm rules, and a digest), so a damaged or tampered item is never returned.
  * Least privilege: the code uses exactly PutItem and GetItem. The template grants exactly those two actions on this one table.
  * Persistence is optional and off by default. With no PROFILE_TABLE the routes answer 503 PERSISTENCE_DISABLED and the core workflow is unaffected.
  * Ids and record contents are never logged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import secrets
import time
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from backend.confirmation import validate_scenario
from backend.errors import ApiError, Throttled, Unavailable, validation_error
from contracts.models import ProfilePutRequest, ProfileResponse, Scenario

log = logging.getLogger("trademind.profiles")
log.setLevel(logging.INFO)

ID_MIN_LENGTH = 32
ID_MAX_LENGTH = 128  # the route and the contract allow up to 128
ID_MIN_DISTINCT = 10
ID_MIN_ENTROPY_BITS = 3.0
ID_MAX_SEQUENCE_RUN = 5
_ID_RE = re.compile(rf"^[A-Za-z0-9_-]{{{ID_MIN_LENGTH},{ID_MAX_LENGTH}}}$")
KEY_PREFIX = "trademind-profile:"

PURPOSE = "fictional-demo"  # stored with every record as a reminder of the only permitted use
DISABLED_MESSAGE = "Profile storage is not enabled on this deployment. The rest of the application works without it."


# --------------------------------------------------------------------------- ids
def generate_profile_id() -> str:
    """A fresh unguessable id: 24 random bytes = 192 bits, 32 URL-safe characters."""
    return secrets.token_urlsafe(24)


def _entropy_bits(text: str) -> float:
    counts = Counter(text)
    return -sum((n / len(text)) * math.log2(n / len(text)) for n in counts.values())


def _longest_sequential_run(text: str) -> int:
    best = run = 1
    direction = 0
    for previous, current in zip(text, text[1:], strict=False):
        step = ord(current) - ord(previous)
        if step in (1, -1) and (direction in (0, step)):
            run, direction = run + 1, step
        else:
            run, direction = (2, step) if step in (1, -1) else (1, 0)
        best = max(best, run)
    return best


def _is_repeating(text: str) -> bool:
    return any(len(text) % size == 0 and text == text[:size] * (len(text) // size) for size in range(1, len(text) // 2 + 1))


# Words that make an id guessable. A random 32-character id contains one of these with probability around 0.03%,
# in which case the client simply generates another. Not a dictionary check: nothing here can detect an arbitrary passphrase.
_GUESSABLE = ("demo", "test", "example", "sample", "profile", "password", "passwd", "secret", "trademind", "canonical",
              "admin", "user", "devco", "designco", "copyco", "123e4567", "qwerty", "letmein")


def id_problem(profile_id: str) -> str | None:
    """A fixed reason code if the id is not acceptable, else None. Never echoes the id."""
    if not _ID_RE.fullmatch(profile_id):
        return "format"
    lowered = profile_id.lower()
    if any(word in lowered for word in _GUESSABLE):
        return "guessable_word"
    if len(set(profile_id)) < ID_MIN_DISTINCT:
        return "too_few_distinct_characters"
    if _entropy_bits(profile_id) < ID_MIN_ENTROPY_BITS:
        return "low_entropy"
    if _longest_sequential_run(profile_id.lower()) > ID_MAX_SEQUENCE_RUN:
        return "sequence"
    if _is_repeating(profile_id):
        return "repetition"
    return None


def validate_profile_id(profile_id: str) -> None:
    if id_problem(profile_id) is not None:
        raise validation_error(
            f"The profile id is not acceptable. Use a random id of at least {ID_MIN_LENGTH} URL-safe characters "
            "(letters, digits, '-' and '_') from a cryptographic random source.",
            ["id"],
        )


def storage_key(profile_id: str) -> str:
    return hashlib.sha256((KEY_PREFIX + profile_id).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- fictional-data guard (best effort)
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_URL = re.compile(r"(https?://|www\.)", re.IGNORECASE)
_PHONE_LIKE = re.compile(r"(?:\+?\d[\s().-]*){9,}")


def looks_personal(text: str) -> bool:
    return bool(_EMAIL.search(text) or _URL.search(text) or _PHONE_LIKE.search(text))


def check_fictional(scenario: Scenario) -> None:
    suspicious = [f"scenario.businesses[{i}].name" for i, b in enumerate(scenario.businesses) if looks_personal(b.name)]
    if suspicious:
        raise ApiError(
            422,
            "PERSONAL_DATA_SUSPECTED",
            "Business names that look like an email address, web address or phone number are not accepted. "
            "Profiles are for fictional demo data only.",
            suspicious,
        )


# --------------------------------------------------------------------------- canonical form and digest
def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- DynamoDB error mapping
_THROTTLE = {"ProvisionedThroughputExceededException", "ThrottlingException", "RequestLimitExceeded", "TransactionInProgressException"}
_DENIED = {"AccessDeniedException", "UnrecognizedClientException", "ExpiredTokenException", "InvalidSignatureException"}
_MISSING_TABLE = {"ResourceNotFoundException"}
_SERVICE = {"InternalServerError", "ServiceUnavailable", "ServiceUnavailableException"}


def map_client_error(exc: ClientError) -> ApiError:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    if code in _THROTTLE:
        return Throttled("Profile storage is throttled. Retry shortly.")
    if code in _DENIED:
        return Unavailable("The server is not allowed to use profile storage. An administrator must check permissions.")
    if code in _MISSING_TABLE:
        return Unavailable("The profile table was not found. An administrator must check the deployment.")
    if code in _SERVICE:
        return Unavailable("Profile storage is temporarily unavailable.")
    return Unavailable("Profile storage failed.")


class DynamoClient(Protocol):
    def put_item(self, **kwargs: Any) -> Any: ...
    def get_item(self, **kwargs: Any) -> Any: ...


# --------------------------------------------------------------------------- the store
class ProfileStore:
    """Adapter pair for Adapters.put_profile / Adapters.get_profile."""

    def __init__(
        self,
        table: str,
        ttl_seconds: int,
        client: DynamoClient | None = None,
        region: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._table = table
        self._ttl = ttl_seconds
        self._client = client
        self._region = region
        self._clock = clock

    def _dynamo(self) -> DynamoClient:
        if self._client is None:
            try:
                self._client = boto3.client(
                    "dynamodb",
                    region_name=self._region,
                    config=Config(
                        connect_timeout=2,
                        read_timeout=3,
                        retries={"total_max_attempts": 2, "mode": "standard"},
                        user_agent_extra="trademind-b7",
                    ),
                )
            except BotoCoreError:
                raise Unavailable("Profile storage is not configured: no AWS region is available.") from None
        return self._client

    # ---- PUT
    def put(self, profile_id: str, request: ProfilePutRequest) -> ProfileResponse:
        validate_profile_id(profile_id)
        validate_scenario(request.scenario)
        check_fictional(request.scenario)

        scenario_json = canonical_json(request.scenario.model_dump(mode="json"))
        now = self._clock()
        item = {
            "pk": {"S": storage_key(profile_id)},
            "scenario": {"S": scenario_json},
            "digest": {"S": digest_of(scenario_json)},
            "schemaVersion": {"S": "1"},
            "purpose": {"S": PURPOSE},
            "createdAt": {"S": datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")},
            "expiresAt": {"N": str(int(now) + self._ttl)},  # the table's TTL attribute: epoch seconds
        }
        try:
            self._dynamo().put_item(TableName=self._table, Item=item, ConditionExpression="attribute_not_exists(pk)")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                log.info(json.dumps({"event": "profile_exists"}))
                raise ApiError(409, "PROFILE_EXISTS", "A profile with this id already exists. Profiles are never overwritten; choose a new id.") from None
            error = map_client_error(exc)
            log.error(json.dumps({"event": "profile_put_failed", "awsCode": str(exc.response.get("Error", {}).get("Code")), "status": error.status}))
            raise error from None
        except BotoCoreError as exc:
            log.error(json.dumps({"event": "profile_put_failed", "errorType": type(exc).__name__}))
            raise Unavailable("Profile storage could not be reached.") from None
        log.info(json.dumps({"event": "profile_stored", "ttlSeconds": self._ttl}))
        return ProfileResponse.model_validate({"id": profile_id, "scenario": request.scenario.model_dump(mode="json")})

    # ---- GET
    def get(self, profile_id: str) -> ProfileResponse:
        validate_profile_id(profile_id)
        try:
            response = self._dynamo().get_item(TableName=self._table, Key={"pk": {"S": storage_key(profile_id)}}, ConsistentRead=True)
        except ClientError as exc:
            error = map_client_error(exc)
            log.error(json.dumps({"event": "profile_get_failed", "awsCode": str(exc.response.get("Error", {}).get("Code")), "status": error.status}))
            raise error from None
        except BotoCoreError as exc:
            log.error(json.dumps({"event": "profile_get_failed", "errorType": type(exc).__name__}))
            raise Unavailable("Profile storage could not be reached.") from None

        item = response.get("Item") if isinstance(response, Mapping) else None
        if not item:
            raise _not_found()
        try:
            expires_at = int(item["expiresAt"]["N"])
        except (KeyError, TypeError, ValueError):
            return self._corrupt("expiry")
        if expires_at <= self._clock():
            # DynamoDB TTL deletion can lag by up to 48 hours: the application enforces expiry itself.
            raise _not_found()
        try:
            scenario_json = item["scenario"]["S"]
            stored_digest = item["digest"]["S"]
        except (KeyError, TypeError):
            return self._corrupt("fields")
        if digest_of(scenario_json) != stored_digest:
            return self._corrupt("digest")
        try:
            scenario = Scenario.model_validate(json.loads(scenario_json))
            validate_scenario(scenario)
        except (ValueError, ApiError):
            return self._corrupt("scenario")
        return ProfileResponse.model_validate({"id": profile_id, "scenario": scenario.model_dump(mode="json")})

    @staticmethod
    def _corrupt(reason: str) -> Any:
        log.error(json.dumps({"event": "profile_corrupt", "reason": reason}))
        raise ApiError(500, "INTERNAL_ERROR", "Internal server error.")


def _not_found() -> ApiError:
    # One body for "never existed", "expired" and "expired but not yet deleted": nothing to tell them apart.
    return ApiError(404, "PROFILE_NOT_FOUND", "No profile exists with this id (it may have expired).")


def disabled_adapter(*_args: Any) -> Any:
    raise ApiError(503, "PERSISTENCE_DISABLED", DISABLED_MESSAGE)


def build_profile_adapters(
    table: str | None, ttl_seconds: int, region: str | None = None, client: DynamoClient | None = None
) -> tuple[Callable[..., Any], Callable[..., Any]]:
    """(put_profile, get_profile). Both answer 503 PERSISTENCE_DISABLED when no table is configured. Never an in-memory substitute."""
    if not table:
        return disabled_adapter, disabled_adapter
    store = ProfileStore(table, ttl_seconds, client=client, region=region)
    return store.put, store.get
