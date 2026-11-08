"""
TradeMind v1 — optional fairness and risk (sensitivity) analysis.
Owner: Developer 1 (optimizer/** and tests/optimizer/**).

Public surface (A10):
  evaluate_risk(scenario, perturbations) → RiskReport

Design philosophy
-----------------
This is SENSITIVITY analysis, NOT probability. It perturbs supplied estimates
by EXPLICIT, user-selected, bounded scenarios, reruns the solver on each, and
reports the worst OBSERVED outcome across exactly those perturbations.

It makes NO probabilistic confidence claims. There is no assumed distribution,
no "95% confidence", no expected value. We only report what each explicit
perturbation actually produced when rerun.

The NOMINAL result (zero perturbation) is computed and its certificate is
preserved unchanged — risk analysis never mutates or re-signs the nominal
result. The core objective (minimise effort, no-worse-off) is unchanged.

A "perturbation" is a bounded multiplier or delta applied to one or more
estimate minutes. The caller supplies them explicitly; this module does not
invent them. Each perturbation is applied to a deep copy of the scenario via
the A6 apply_changes path (estimate changes), so the original is never mutated.

RiskReport records:
  - nominal_status and nominal_net_savings (the certified baseline)
  - per-perturbation observed status and net savings
  - worst observed net savings across all tested perturbations
  - whether any tested perturbation drove the scenario infeasible/no_exchange
  - explicit statement that this is sensitivity over a finite tested set,
    not a probabilistic risk estimate

All integer minutes. No floats in the model. Multipliers are applied with
integer arithmetic (floor division avoided by requiring integer results or
explicit rounding stated by the caller — see EstimatePerturbation).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from contracts.models import (
    EstimateChange,
    Result,
    Scenario,
    ServiceName,
)
from optimizer.engine import solve as _solve
from optimizer.simulation import apply_changes as _apply_changes
from optimizer.simulation import SimulationError


# ---------------------------------------------------------------------------
# Perturbation specification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EstimatePerturbation:
    """
    One explicit, bounded perturbation of a business's estimate for a service.

    The new minutes value is supplied directly as an integer (new_minutes),
    so there is no hidden floating-point rounding. The caller is responsible
    for computing e.g. "20% higher" into an explicit integer before passing it.

    This keeps the model fully integer and deterministic.
    """
    businessId: str
    service: str          # "coding" | "design" | "writing"
    new_minutes: int      # explicit new estimate (>= 1)


@dataclass(frozen=True)
class PerturbationScenario:
    """
    A named bundle of estimate perturbations applied together as one what-if.
    """
    label: str
    changes: tuple[EstimatePerturbation, ...]


# ---------------------------------------------------------------------------
# Risk report
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PerturbationOutcome:
    """Observed result of one perturbation scenario after an actual rerun."""
    label: str
    valid_patch: bool
    status: str                     # "collaboration" | "no_exchange" | "infeasible" | "rejected"
    net_savings: int | None         # total net savings if collaboration, else None
    rejection_reason: str = ""


@dataclass
class RiskReport:
    """
    Sensitivity analysis over a finite, explicit set of estimate perturbations.

    nominal_status        — status of the unperturbed scenario.
    nominal_net_savings   — total net savings of the unperturbed scenario (None if not collaboration).
    nominal_certificate   — the certified nominal certificate (unchanged).
    outcomes              — one PerturbationOutcome per tested perturbation.
    note                  — explicit sensitivity-not-probability disclaimer.
    """
    nominal_status: str
    nominal_net_savings: int | None
    nominal_certificate: object  # Certificate | None — preserved unchanged
    outcomes: list[PerturbationOutcome] = field(default_factory=list)
    note: str = (
        "Sensitivity analysis over the finite, explicitly supplied perturbations "
        "only. This is NOT a probabilistic risk estimate: no distribution is "
        "assumed and no confidence level is implied. Worst-case figures are the "
        "worst OBSERVED outcome among the tested perturbations, not a bound over "
        "all possible perturbations."
    )

    @property
    def worst_net_savings(self) -> int | None:
        """
        Worst (lowest) net savings observed across collaboration outcomes.
        Returns None if no tested perturbation produced collaboration.
        """
        collab = [o.net_savings for o in self.outcomes
                  if o.status == "collaboration" and o.net_savings is not None]
        return min(collab) if collab else None

    @property
    def any_perturbation_breaks_collaboration(self) -> bool:
        """True if any tested perturbation produced a non-collaboration status."""
        return any(o.status in ("no_exchange", "infeasible") for o in self.outcomes)

    @property
    def tested_count(self) -> int:
        return len(self.outcomes)


# ---------------------------------------------------------------------------
# evaluate_risk
# ---------------------------------------------------------------------------

def evaluate_risk(
    scenario: Scenario,
    perturbations: list[PerturbationScenario],
) -> RiskReport:
    """
    Evaluate scenario sensitivity to a finite, explicit set of estimate perturbations.

    For each PerturbationScenario:
      - Convert its EstimatePerturbations to EstimateChange objects.
      - apply_changes to a deep copy (original never mutated).
      - Rerun the solver.
      - Record the observed status and net savings.

    The nominal (zero-perturbation) result is computed first and its certificate
    is preserved in the report unchanged — risk analysis never re-signs it.

    Returns a RiskReport. Makes NO probabilistic claims.
    """
    # Nominal result — compute once, preserve certificate unchanged
    nominal = _solve(scenario)
    nominal_status = nominal.status.value
    nominal_net = nominal.totals.netSavingsMinutes if nominal.totals is not None else None
    nominal_cert = nominal.certificate  # preserved as-is

    outcomes: list[PerturbationOutcome] = []

    for pert in perturbations:
        # Convert to EstimateChange objects
        try:
            changes = [
                EstimateChange(
                    kind="estimate",
                    businessId=p.businessId,
                    service=ServiceName(p.service),
                    minutes=p.new_minutes,
                )
                for p in pert.changes
            ]
        except Exception as exc:
            outcomes.append(PerturbationOutcome(
                label=pert.label,
                valid_patch=False,
                status="rejected",
                net_savings=None,
                rejection_reason=f"invalid perturbation: {exc}",
            ))
            continue

        # Apply to a deep copy (original untouched)
        try:
            patched = _apply_changes(scenario, changes)
        except SimulationError as exc:
            outcomes.append(PerturbationOutcome(
                label=pert.label,
                valid_patch=False,
                status="rejected",
                net_savings=None,
                rejection_reason=str(exc),
            ))
            continue

        # Rerun solver
        rerun = _solve(patched)
        status = rerun.status.value
        net = rerun.totals.netSavingsMinutes if rerun.totals is not None else None

        outcomes.append(PerturbationOutcome(
            label=pert.label,
            valid_patch=True,
            status=status,
            net_savings=net,
        ))

    return RiskReport(
        nominal_status=nominal_status,
        nominal_net_savings=nominal_net,
        nominal_certificate=nominal_cert,
        outcomes=outcomes,
    )
