"""
TradeMind v1 — deterministic what-if engine.
Owner: Developer 1 (optimizer/** and tests/optimizer/**).

Public surface (A6):
  apply_changes(scenario, changes)        → Scenario  (patched deep copy)
  diff_results(before, after)             → ResultDiff (UI/API helper)

Design
------
apply_changes
  Applies a validated list of Change objects to a DEEP COPY of the scenario.
  The original Scenario is NEVER mutated.

  Allowlisted paths only (explicit, not eval-based):
    kind='capacity'    → business.capacityMinutes
    kind='coordination'→ business.coordinationMinutes
    kind='estimate'    → business.estimates[service].minutes
                         (creates estimate entry if service not present)

  Validation:
    - Each businessId must exist in the scenario.
    - For 'estimate': service must be a valid ServiceName enum value.
    - Numeric values: capacity/coordination ≥ 0 (NonNegativeMinutes);
      estimate minutes ≥ 1 (Minutes). All must be integers.
    - Duplicate targets rejected: the same (kind, businessId) or
      (kind=estimate, businessId, service) pair may not appear twice.
      If duplicates exist, the WHOLE patch is rejected atomically — no
      partial application.
    - No language parsing — apply_changes never calls Bedrock or interprets
      text. Text interpretation is the backend's responsibility (B6).

  After applying, the patched scenario is re-validated through Pydantic so
  extra-field checks and schema invariants are re-enforced.  Returns the
  validated Scenario.

  Raises SimulationError (a subclass of ValueError) with a descriptive
  message on any validation failure.  The original scenario is untouched.

diff_results
  Compares two Result objects and returns a ResultDiff summary for
  UI/API callers.  Does NOT change the Result wire schema — it is a
  helper only.  Returns a lightweight dataclass with per-business deltas
  and a top-level summary.

Notes:
  - apply_changes does not run the solver.  The caller (backend B6) runs
    optimize() on the returned patched scenario.
  - No floats.  All integer minutes.
  - The original scenario is preserved for comparison; the patched copy
    is a separate object.
"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field

from optimizer.contract import (
    CapacityChange,
    Change,
    CoordinationChange,
    EstimateChange,
    Result,
    ResultStatus,
    Scenario,
    ServiceEstimate,
    ServiceName,
)


# ---------------------------------------------------------------------------
# SimulationError
# ---------------------------------------------------------------------------

class SimulationError(ValueError):
    """
    Raised by apply_changes on any validation failure.
    The original scenario is guaranteed untouched when this is raised.
    """
    def __init__(self, message: str, fields: list[str] | None = None):
        super().__init__(message)
        self.fields = fields or []


# ---------------------------------------------------------------------------
# apply_changes
# ---------------------------------------------------------------------------

# Allowlist of supported change kinds
_SUPPORTED_KINDS = {"capacity", "coordination", "estimate"}


def apply_changes(scenario: Scenario, changes: list[Change]) -> Scenario:
    """
    Apply a validated list of Change objects to a deep copy of the scenario.

    The original scenario is NEVER mutated.
    Raises SimulationError atomically if any validation fails.

    Allowed paths:
      capacity    → business.capacityMinutes    (non-negative integer)
      coordination→ business.coordinationMinutes (non-negative integer)
      estimate    → business.estimates[service].minutes (positive integer)

    No language parsing is performed here.
    """
    if not changes:
        # No changes → return a validated copy of the original
        return Scenario.model_validate(json.loads(scenario.model_dump_json()))

    # ---- Step 1: Validate all changes BEFORE applying any (atomic) ----------
    business_ids = {b.id for b in scenario.businesses}

    # Duplicate target detection
    seen_targets: set[tuple] = set()
    errors: list[str] = []

    for i, change in enumerate(changes):
        kind = change.kind

        if kind not in _SUPPORTED_KINDS:
            errors.append(f"changes[{i}]: unsupported kind {kind!r}")
            continue

        business_id = change.businessId
        if business_id not in business_ids:
            errors.append(f"changes[{i}]: unknown businessId {business_id!r}")
            continue

        if kind in ("capacity", "coordination"):
            target = (kind, business_id)
            if change.minutes < 0:
                errors.append(
                    f"changes[{i}]: {kind} minutes must be >= 0, got {change.minutes}"
                )
        else:  # estimate
            assert isinstance(change, EstimateChange)
            # Service is already a ServiceName enum from Pydantic validation
            target = ("estimate", business_id, change.service.value)
            if change.minutes < 1:
                errors.append(
                    f"changes[{i}]: estimate minutes must be >= 1, got {change.minutes}"
                )

        if target in seen_targets:
            errors.append(
                f"changes[{i}]: duplicate target {target} — each target may appear at most once"
            )
        else:
            seen_targets.add(target)

    if errors:
        raise SimulationError(
            f"Invalid changes — patch rejected atomically: {'; '.join(errors)}",
            fields=errors,
        )

    # ---- Step 2: Deep-copy and apply ----------------------------------------
    # Use JSON round-trip for a clean deep copy (avoids reference sharing)
    patched_data: dict = json.loads(scenario.model_dump_json())

    # Build index: {businessId: index in patched_data["businesses"]}
    biz_index = {b["id"]: i for i, b in enumerate(patched_data["businesses"])}

    for change in changes:
        b_idx = biz_index[change.businessId]
        biz = patched_data["businesses"][b_idx]

        if change.kind == "capacity":
            biz["capacityMinutes"] = change.minutes

        elif change.kind == "coordination":
            biz["coordinationMinutes"] = change.minutes

        else:  # estimate
            assert isinstance(change, EstimateChange)
            svc_value = change.service.value
            ests = biz["estimates"]
            # Update existing estimate or add new one
            for est in ests:
                if est["service"] == svc_value:
                    est["minutes"] = change.minutes
                    break
            else:
                ests.append({"service": svc_value, "minutes": change.minutes})

    # ---- Step 3: Re-validate through Pydantic --------------------------------
    try:
        return Scenario.model_validate(patched_data)
    except Exception as exc:
        raise SimulationError(
            f"Patched scenario failed validation: {exc}",
            fields=["scenario"],
        ) from exc


# ---------------------------------------------------------------------------
# ResultDiff
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BusinessDelta:
    """Change in one business's metrics between two Results."""
    businessId: str
    baselineDelta: int           # typically 0 (baseline changes only if estimates change)
    effortDelta: int             # negative = less effort (better)
    netSavingsDelta: int         # positive = more savings (better)
    statusChanged: bool          # business went from present to absent or vice-versa


@dataclass
class ResultDiff:
    """
    Comparison of two Results (before and after a what-if change).
    Used by the backend /simulate response and the UI to display deltas.

    Does NOT change the Result wire schema — this is a helper type only.
    """
    before_status: str
    after_status: str
    status_changed: bool

    # Per-business deltas (only for businesses present in both results)
    business_deltas: list[BusinessDelta] = field(default_factory=list)

    # Aggregate deltas (None when before/after totals are null)
    total_effort_delta: int | None = None
    total_net_savings_delta: int | None = None
    gross_savings_delta: int | None = None

    @property
    def improved(self) -> bool:
        """True if after is better than before (more net savings or feasible where was not)."""
        if self.before_status == "infeasible" and self.after_status != "infeasible":
            return True
        if self.after_status == "infeasible":
            return False
        if self.total_net_savings_delta is not None:
            return self.total_net_savings_delta > 0
        return False

    @property
    def degraded(self) -> bool:
        """True if after is strictly worse than before."""
        if self.after_status == "infeasible" and self.before_status != "infeasible":
            return True
        if self.total_net_savings_delta is not None:
            return self.total_net_savings_delta < 0
        return False


def diff_results(before: Result, after: Result) -> ResultDiff:
    """
    Compare two Results and return a ResultDiff.

    Does NOT mutate either Result.
    Works across all status combinations (collaboration/no_exchange/infeasible).
    """
    before_status = before.status.value
    after_status  = after.status.value

    # Aggregate deltas
    total_effort_delta: int | None = None
    total_net_savings_delta: int | None = None
    gross_savings_delta: int | None = None

    if before.totals is not None and after.totals is not None:
        total_effort_delta      = after.totals.effortMinutes      - before.totals.effortMinutes
        total_net_savings_delta = after.totals.netSavingsMinutes  - before.totals.netSavingsMinutes
        gross_savings_delta     = after.totals.grossSavingsMinutes - before.totals.grossSavingsMinutes
    elif before.totals is None and after.totals is not None:
        total_effort_delta      = after.totals.effortMinutes
        total_net_savings_delta = after.totals.netSavingsMinutes
        gross_savings_delta     = after.totals.grossSavingsMinutes
    elif before.totals is not None and after.totals is None:
        total_effort_delta      = -before.totals.effortMinutes
        total_net_savings_delta = -before.totals.netSavingsMinutes
        gross_savings_delta     = -before.totals.grossSavingsMinutes

    # Per-business deltas
    business_deltas: list[BusinessDelta] = []
    if before.businessMetrics is not None and after.businessMetrics is not None:
        before_by_id = {m.businessId: m for m in before.businessMetrics}
        after_by_id  = {m.businessId: m for m in after.businessMetrics}
        all_ids = sorted(set(before_by_id) | set(after_by_id))

        for bid in all_ids:
            bm = before_by_id.get(bid)
            am = after_by_id.get(bid)
            if bm is not None and am is not None:
                business_deltas.append(BusinessDelta(
                    businessId=bid,
                    baselineDelta=am.baselineMinutes   - bm.baselineMinutes,
                    effortDelta=am.effortMinutes       - bm.effortMinutes,
                    netSavingsDelta=am.netSavingsMinutes - bm.netSavingsMinutes,
                    statusChanged=False,
                ))
            else:
                business_deltas.append(BusinessDelta(
                    businessId=bid,
                    baselineDelta=0,
                    effortDelta=0,
                    netSavingsDelta=0,
                    statusChanged=True,
                ))

    return ResultDiff(
        before_status=before_status,
        after_status=after_status,
        status_changed=(before_status != after_status),
        business_deltas=business_deltas,
        total_effort_delta=total_effort_delta,
        total_net_savings_delta=total_net_savings_delta,
        gross_savings_delta=gross_savings_delta,
    )
