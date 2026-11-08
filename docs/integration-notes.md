# Integration notes: how the optimizer, backend and frontend fit together

Written by Developer 2 (maintainer) when merging the three tracks. **For Developer 1: please read the section about your files.**

## What was wrong when the three branches first met

Developer 1's optimizer (A2 to A10) was pushed to `main` and could not run against the repository's shared contract:

1. It imported names that have never existed in `contracts/models.py` (`ServiceName`, `ResultStatus`, `CapacityChange`, `CoordinationChange`, `ServiceEstimate`), so 8 of its modules failed to import.
   The shared, generated models wrap every primitive in a `RootModel` (`Slug`, `Service`, ...), which is unhashable and does not compare equal to a plain string, so the solver could not simply use them as dictionary keys.
2. There was no `optimizer/api.py`, the file the backend imports. `optimizer/__init__.py` was still the Phase 0 placeholder (`STATUS = "not_implemented"`).
3. Its tests expected fixtures shaped `{"scenario": ...}` and a `disruption.json` that is itself a scenario. The frozen fixtures are bare scenarios, and `disruption.json` lists changes to apply to the canonical scenario.
4. For `no_exchange` it returned empty assignments and null metrics, totals and certificate. The frozen schema allows nulls **only** for `infeasible`; every other status must carry assignments, metrics, totals and a certificate. The frontend's `isResult` enforces the same, so the UI would have rejected the disruption demo.
5. Its second tie-break compared `effort * total_baseline`, which orders businesses by raw effort, not by `effort / baseline`. Two plans with equal total effort could be ranked wrongly. (Found by running the solver against an independent brute-force reference on 907 random scenarios: one disagreement, now zero.)
6. Its "independent oracle" tests copied the same flawed formula ("same logic as engine"), so they could not catch point 5.

## What changed (Developer 1's files were edited; `contracts/` was not)

| Change | Where | Why |
|---|---|---|
| New plain-typed models (`str`, `int`, `Enum`), strict like the contract | `optimizer/contract.py` | The optimizer needs hashable ids and comparable statuses. Same field names and limits as the v1 wire format. Business count 3 to 5 here, because A9's extended mode needs it; the shared schema still allows exactly 3 and the backend never enables extended mode |
| Imports repointed from `contracts.models` to `optimizer.contract` | every `optimizer/*.py` and `tests/optimizer/*.py` | Mechanical |
| `optimizer/api.py` with `optimize(scenario: dict) -> dict`, `simulate(scenario, changes)`, `verify(scenario, result)` | new | The seam `backend/optimization.py` imports. Raises `MissingBaselineError` (422 MISSING_BASELINE), `OptimizerInputError` (422), `SimulationError` (422) |
| `solve` keeps the best feasible allocation overall and labels it `collaboration` only if strictly beneficial; otherwise `no_exchange` with the least-effort valid plan, metrics, totals and a certificate | `optimizer/engine.py` | Frozen schema |
| `verify` recomputes the **status** too, and refuses an "infeasible" claim for a scenario that has a valid allocation | `optimizer/engine.py` | A client must not be able to relabel a plan |
| Certificates come from the independent `generate_certificate` (A5) | `optimizer/engine.py` | The engine's older hard-coded certificate listed `at_least_one_cross_business_assignment` and `total_net_savings_positive` as passed checks, which would be false for `no_exchange` |
| Exact ratio comparison (`RatioRank`, integer cross multiplication, zero baseline ranks highest) | `optimizer/ratio.py`, used by `engine.py` and `metrics.py` | Contract tie-break |
| Fixture helper that presents the frozen fixtures in the shape the tests use, and derives the disruption and infeasible scenarios by applying the fixture's own change lists with plain dictionary edits | `tests/optimizer/_fixtures.py` | Tests no longer depend on a different fixture layout |
| Tests updated: `no_exchange` now asserts the self-production plan (known from the fixture: each business's own need, 1,440 baseline, zero savings); oracles use `fractions.Fraction`; a regression test pins the case the differential run found | `tests/optimizer/*` | Tests must encode the contract |
| `optimizer/__init__.py` now exports the real entry points | `optimizer/__init__.py` | The placeholder claimed "not implemented" |

Backend test changes (Developer 2's area): the deploy sandbox starts without `optimizer/api.py` because the real repo now has one; the cross-track test compares `businessMetrics` by `businessId` (the contract does not order them, the optimizer sorts by id).

## Things Developer 1 should know

* Their compliance records `docs/compliance/A2.md` to `A10.md` describe the old `no_exchange` shape and the old `contracts.models` imports. They were not edited; please update them and keep the human reviews PENDING until you have actually reviewed the changes above.
* The contract does not specify an order for `businessMetrics`. The optimizer returns them sorted by business id so the answer does not depend on the order businesses were typed in.
* `no_exchange` plans are valid, certified, and show zero net savings in total. They are not "empty".
* A9 (four and five companies) is gated off by default and the shared schema allows exactly three. Nothing in the backend or frontend enables it.
* Passing tests are not the whole story: the 907-scenario comparison with `tests/backend/reference_optimizer.py` is the check that found the tie-break problem. Keep an independent oracle that does not share code with the solver.

## What was checked on the merged code (2026-11-07, Windows)

| Check | Result |
|---|---|
| `python -m pytest tests/optimizer tests/backend` | 1,208 passed, 2 skipped, 2 xfailed. The 2 skips are backend tests that only run when `optimizer.api` is absent. The 2 xfails are the known Pydantic limit |
| Optimizer against the independent reference, 2,500 random scenarios (1,593 had no owner baseline and were skipped as invalid) | 907 comparable, 907 identical (status, assignments) |
| `python scripts/generate_contracts.py --check`, `sam validate --lint` | pass |
| `npm --prefix frontend ci`, `run typecheck`, `test -- --run`, `run build` | pass; 333 tests in 13 files |
| `npm --prefix frontend run e2e` | 68 passed, 1 skipped (screenshots, only with `C8_SHOTS_DIR`) |
| Canonical scenario through the production routes with the real optimizer | 1,440 to 540, 900 saved, 300 each; coordination 60 gives effort 720 and net savings 720; designco design 600 gives `no_exchange`; all capacities 0 gives `infeasible`; missing baseline gives 422 `MISSING_BASELINE` |

**Not checked:** the deployed AWS stack has NOT been updated with the optimizer (it still answers 503 on `/optimize`, `/simulate`, `/explain` until redeployed); runtime of the 5-business extended mode on Lambda; any browser run against a hosted URL.
