"""
TradeMind v1 — optimizer-side extended-mode configuration and gating.
Owner: Developer 1 (optimizer/** and tests/optimizer/**).

Purpose (A9)
------------
The shared contract (contracts/models.py, owned by Developer 2) already permits
3..5 businesses and 1..6 deliverables as a PREDECLARED optional extension.
This module adds the optimizer-side GATE that decides whether a scenario with
more than the MVP default of 3 businesses is accepted.

It does NOT modify the shared contract. It is a thin policy layer:

  - DEFAULT mode: exactly 3 businesses (the MVP). 4- or 5-business scenarios
    are rejected with a clear error unless extended mode is explicitly enabled.
  - EXTENDED mode: 3..5 businesses allowed, still bounded to 1..6 deliverables
    and the hard 5^6 = 15625 candidate cap.

Extended mode is OFF by default, so existing 3-business behaviour is unchanged.
It is enabled per-call via an argument (no global mutable state, no env var
required) so the backend can activate it without modifying any optimizer file.

Public surface (A9):
  OptimizerConfig                     — immutable config dataclass
  MAX_MVP_BUSINESSES = 3
  MAX_EXTENDED_BUSINESSES = 5
  MAX_DELIVERABLES = 6
  MAX_CANDIDATES = 15625
  validate_scenario_bounds(scenario, config) → None  (raises ExtendedModeError)
  candidate_count_upper_bound(scenario)       → int
"""
from __future__ import annotations

from dataclasses import dataclass

from optimizer.contract import Scenario

# Hard limits (from the frozen contract)
MAX_MVP_BUSINESSES: int = 3
MAX_EXTENDED_BUSINESSES: int = 5
MIN_BUSINESSES: int = 3
MAX_DELIVERABLES: int = 6
MIN_DELIVERABLES: int = 1
MAX_CANDIDATES: int = 5 ** 6  # 15625


class ExtendedModeError(ValueError):
    """
    Raised when a scenario exceeds the bounds permitted by the active config.
    Maps to HTTP 422 VALIDATION_ERROR in the backend.
    """
    def __init__(self, message: str, fields: list[str] | None = None):
        super().__init__(message)
        self.fields = fields or []


@dataclass(frozen=True)
class OptimizerConfig:
    """
    Immutable optimizer configuration.

    extended_mode = False  → MVP: exactly 3 businesses.
    extended_mode = True   → allow 3..5 businesses (still ≤6 deliverables, ≤15625 candidates).

    Default is the MVP (extended_mode False), so three-business behaviour is
    preserved unless a caller explicitly opts in.
    """
    extended_mode: bool = False

    @property
    def max_businesses(self) -> int:
        return MAX_EXTENDED_BUSINESSES if self.extended_mode else MAX_MVP_BUSINESSES


# Default configuration instance (MVP, three businesses).
DEFAULT_CONFIG = OptimizerConfig(extended_mode=False)


def candidate_count_upper_bound(scenario: Scenario) -> int:
    """
    Compute the exact upper bound on candidate assignments for the scenario:
    product over deliverables of the number of feasible providers.

    This is <= businesses ** deliverables <= 5^6 = 15625 for valid scenarios.
    """
    estimates = {
        b.id: {e.service for e in b.estimates}
        for b in scenario.businesses
    }
    total = 1
    for d in scenario.deliverables:
        owner = d.ownerId
        n_providers = 1  # owner always a candidate (if baseline present)
        for b in scenario.businesses:
            if b.id != owner and d.service in estimates.get(b.id, set()):
                n_providers += 1
        total *= n_providers
    return total


def validate_scenario_bounds(scenario: Scenario, config: OptimizerConfig = DEFAULT_CONFIG) -> None:
    """
    Enforce the optimizer's business-count policy and the hard candidate cap.

    Raises ExtendedModeError if:
      - business count exceeds the active config's maximum
        (3 in MVP mode, 5 in extended mode), OR
      - business count is below the minimum (3), OR
      - deliverable count is outside 1..6, OR
      - the exact candidate upper bound exceeds 15625.

    The shared Pydantic contract already rejects >5 businesses and >6
    deliverables at parse time; this function adds the MVP-vs-extended gate
    and the explicit candidate-cap check on top of that.
    """
    n_biz = len(scenario.businesses)
    n_del = len(scenario.deliverables)

    if n_biz < MIN_BUSINESSES:
        raise ExtendedModeError(
            f"Scenario has {n_biz} businesses; minimum is {MIN_BUSINESSES}.",
            fields=["businesses"],
        )

    if n_biz > config.max_businesses:
        if not config.extended_mode and n_biz <= MAX_EXTENDED_BUSINESSES:
            raise ExtendedModeError(
                f"Scenario has {n_biz} businesses. The MVP default supports exactly "
                f"{MAX_MVP_BUSINESSES}. Enable extended mode (OptimizerConfig("
                f"extended_mode=True)) to allow up to {MAX_EXTENDED_BUSINESSES}.",
                fields=["businesses"],
            )
        raise ExtendedModeError(
            f"Scenario has {n_biz} businesses; maximum is {config.max_businesses}.",
            fields=["businesses"],
        )

    if not (MIN_DELIVERABLES <= n_del <= MAX_DELIVERABLES):
        raise ExtendedModeError(
            f"Scenario has {n_del} deliverables; allowed range is "
            f"{MIN_DELIVERABLES}..{MAX_DELIVERABLES}.",
            fields=["deliverables"],
        )

    bound = candidate_count_upper_bound(scenario)
    if bound > MAX_CANDIDATES:
        raise ExtendedModeError(
            f"Candidate upper bound {bound} exceeds the hard cap {MAX_CANDIDATES} "
            f"(5^6). Reduce businesses or deliverables.",
            fields=["businesses", "deliverables"],
        )
