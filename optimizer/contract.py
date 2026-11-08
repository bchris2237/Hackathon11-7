"""Plain-typed models for the optimizer (the A1 "strict Pydantic models").

Why this file exists
--------------------
The shared, generated `contracts/models.py` wraps every primitive in a `RootModel` (`Slug`, `Service`, `NonNegativeMinutes`).
Those wrappers are unhashable and do not compare equal to plain strings or ints, which is awkward for a solver that uses ids and
services as dictionary keys and compares statuses. The optimizer therefore keeps its OWN models here, with plain `str`, `int` and
`Enum` fields, and `optimizer/api.py` converts at the boundary: plain dicts in, plain dicts out. The wire format (field names,
limits, the frozen v1 schema) is unchanged, and `tests/optimizer/test_wire_contract.py` checks that results from this package
validate against the shared `contracts.models.Result` and the JSON Schema.

Rules enforced here match the frozen contract: unknown fields rejected, ids are ASCII slugs, minutes are integers (booleans are
refused), unique ids, `ownerId` must name a business, at most one estimate per service per business. The "owner must have a
baseline estimate" rule is NOT a model rule: it is raised by the solver as `MissingBaselineError` (HTTP 422 MISSING_BASELINE).
Business count: the model accepts 3 to 5; `optimizer/models.py` gates anything above 3 behind an explicit extended-mode switch.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

MAX_MINUTES = 1_000_000
SLUG_PATTERN = r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$"

Slug = Annotated[StrictStr, Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)]
PositiveMinutes = Annotated[StrictInt, Field(ge=1, le=MAX_MINUTES)]
NonNegativeMinutes = Annotated[StrictInt, Field(ge=0, le=MAX_MINUTES)]
Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServiceName(str, Enum):
    CODING = "coding"
    DESIGN = "design"
    WRITING = "writing"


class ResultStatus(str, Enum):
    COLLABORATION = "collaboration"
    NO_EXCHANGE = "no_exchange"
    INFEASIBLE = "infeasible"


# ----------------------------------------------------------------------------------------------- scenario
class ServiceEstimate(_Strict):
    service: ServiceName
    minutes: PositiveMinutes


class Business(_Strict):
    id: Slug
    name: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    capacityMinutes: NonNegativeMinutes
    coordinationMinutes: NonNegativeMinutes
    estimates: Annotated[list[ServiceEstimate], Field(max_length=3)]

    @model_validator(mode="after")
    def _unique_services(self) -> Business:
        services = [e.service for e in self.estimates]
        if len(services) != len(set(services)):
            raise ValueError("a business may have at most one estimate per service")
        return self


class Deliverable(_Strict):
    id: Slug
    ownerId: Slug
    service: ServiceName


class Scenario(_Strict):
    schemaVersion: Literal["1"]
    businesses: Annotated[list[Business], Field(min_length=3, max_length=5)]
    deliverables: Annotated[list[Deliverable], Field(min_length=1, max_length=6)]
    confirmed: Literal[True]

    @model_validator(mode="after")
    def _references(self) -> Scenario:
        business_ids = [b.id for b in self.businesses]
        if len(business_ids) != len(set(business_ids)):
            raise ValueError("business ids must be unique")
        deliverable_ids = [d.id for d in self.deliverables]
        if len(deliverable_ids) != len(set(deliverable_ids)):
            raise ValueError("deliverable ids must be unique")
        unknown = [d.id for d in self.deliverables if d.ownerId not in set(business_ids)]
        if unknown:
            raise ValueError("every deliverable's ownerId must name a business")
        return self


# ----------------------------------------------------------------------------------------------- result
class Assignment(_Strict):
    deliverableId: Slug
    providerId: Slug
    minutes: PositiveMinutes


class BusinessMetrics(_Strict):
    businessId: Slug
    baselineMinutes: Annotated[StrictInt, Field(ge=0)]
    productionMinutes: Annotated[StrictInt, Field(ge=0)]
    coordinationMinutes: Annotated[StrictInt, Field(ge=0)]
    effortMinutes: Annotated[StrictInt, Field(ge=0)]
    netSavingsMinutes: StrictInt


class Totals(_Strict):
    baselineMinutes: Annotated[StrictInt, Field(ge=0)]
    productionMinutes: Annotated[StrictInt, Field(ge=0)]
    coordinationMinutes: Annotated[StrictInt, Field(ge=0)]
    effortMinutes: Annotated[StrictInt, Field(ge=0)]
    grossSavingsMinutes: StrictInt
    netSavingsMinutes: StrictInt


class Reason(_Strict):
    code: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    message: Annotated[StrictStr, Field(min_length=1, max_length=1000)]


class CertificateCheck(_Strict):
    name: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    passed: Literal[True]


class Certificate(_Strict):
    schemaVersion: Literal["1"]
    scenarioDigest: Digest
    allocationDigest: Digest
    verification: Literal["valid"]
    assumptions: list[Annotated[StrictStr, Field(min_length=1, max_length=500)]]
    checks: list[CertificateCheck]


class Result(_Strict):
    schemaVersion: Literal["1"]
    status: ResultStatus
    assignments: Annotated[list[Assignment], Field(max_length=6)]
    businessMetrics: Annotated[list[BusinessMetrics] | None, Field(max_length=5)]
    totals: Totals | None
    reasons: list[Reason]
    certificate: Certificate | None


# ----------------------------------------------------------------------------------------------- what-if changes
class CapacityChange(_Strict):
    kind: Literal["capacity"]
    businessId: Slug
    minutes: NonNegativeMinutes


class CoordinationChange(_Strict):
    kind: Literal["coordination"]
    businessId: Slug
    minutes: NonNegativeMinutes


class EstimateChange(_Strict):
    kind: Literal["estimate"]
    businessId: Slug
    service: ServiceName
    minutes: PositiveMinutes


Change = Annotated[CapacityChange | CoordinationChange | EstimateChange, Field(discriminator="kind")]

__all__ = [
    "Assignment", "Business", "BusinessMetrics", "CapacityChange", "Certificate", "CertificateCheck", "Change",
    "CoordinationChange", "Deliverable", "EstimateChange", "Reason", "Result", "ResultStatus", "Scenario",
    "ServiceEstimate", "ServiceName", "Totals",
]
