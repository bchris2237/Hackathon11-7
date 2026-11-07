# Contracts: single source of truth and how to change them

The wire contract (TradeMind v1) is frozen in `GENERAL-GUIDELINES.md`. This repo encodes it once, in
JSON Schema, and generates the language types from it.

| File | Role | Edited by hand? |
|---|---|---|
| `contracts/scenario.schema.json` | `Scenario`, `Business`, `Deliverable`, `Estimate`, shared primitives | Yes |
| `contracts/result.schema.json` | `Result`, `Assignment`, metrics, `Totals`, `Certificate` | Yes |
| `contracts/api.schema.json` | Every request/response, `Change`, `ErrorEnvelope`, `HealthResponse` (all under `$defs`) | Yes |
| `contracts/api.openapi.yaml` | Routes only. References the schemas above, defines no shapes | Yes |
| `contracts/models.py` | Generated Pydantic 2 models | **No** |
| `contracts/models.ts` | Generated TypeScript types | **No** |
| `fixtures/*.json` | Canonical scenario, 60-minute coordination variant, disruption patches, simulated extraction | Yes (with tests) |
| `fixtures/contract-cases.json` | Invalid and valid cases run by both the Python and TypeScript suites | Yes |

`api.schema.json` is an addition to the three schema files named in the guide. The generators need
one place for non-scenario, non-result shapes, and defining them in the OpenAPI file would duplicate them.

## Regenerate and check

```bash
python scripts/generate_contracts.py          # rewrites models.py and models.ts
python scripts/generate_contracts.py --check  # exits 1 if either is stale
```

The script bundles the three schemas into one in memory (the generators cannot follow cross-file
references into a single output), then runs datamodel-code-generator and json-schema-to-typescript.
It needs the Python venv and `npm --prefix frontend ci` done first.
`tests/backend/test_contracts_parity.py` fails if `models.py` is stale; `--check` covers both files.

## Changing a contract (joint decision)

1. Open a request: affected file/interface, reason, desired behavior, compatibility impact, acceptance example.
2. All three developers agree. Developer 2 edits the schema.
3. Add or update cases in `fixtures/contract-cases.json` and any fixture.
4. Run the generator, then every command in the checklist. Commit schema, generated files, fixtures and tests together.
5. Consumers adopt the change only after step 4 is merged. Never hand-edit `models.py` or `models.ts`.

## What JSON Schema and the generated models enforce

Unknown fields rejected; integer minutes only (strict in Pydantic, so `"600"` and `600.5` fail); minutes
bounded to 1000000 with estimates >= 1 and capacity/coordination >= 0; frozen service enumeration;
slug-pattern ids; array size limits; `confirmed` must be `true`; `schemaVersion` must be `"1"`;
`Result` shape rules (an `infeasible` result has empty assignments and null metrics, totals and
certificate; any other status must have them); `Change` accepts only the three tagged shapes
(no JSON paths, no code).

## Rules that are NOT in the schema (validators must enforce them)

These cannot be expressed in JSON Schema. Phase 0 only checks them on the fixtures
(`test_fixture_semantic_rules`). The B-track and A-track validators must enforce them at runtime:

- business ids unique; deliverable ids unique: **enforced since B3** (`validate_scenario` in `backend/confirmation.py`, used by `/confirm` and `/optimize`)
- every `ownerId` references an existing business: **enforced since B3**. Every `Change.businessId` references one: still open (simulate prompt)
- at most one estimate per service per business: **enforced since B3**
- each owner has an estimate for its own required service, otherwise **422 `MISSING_BASELINE`**: **enforced since B3**
  (fields look like `scenario.businesses[2].estimates.coding`)

`fields` paths use one style on every route: names joined by dots, array indexes in brackets
(`scenario.businesses[0].estimates[2].service`). Since B3 this includes schema errors, which used to print
`scenario.businesses.0.estimates.2.service`. A UI needs one parser for `fields` and for `/extract`'s `missingFields` paths.
- `SimulateRequest`: text together with non-empty `changes`, or text with `apply: true`, is rejected.
  The JSON Schema rejects it, but the generated Pydantic model cannot, so two parity cases are marked xfail.
  **Enforced since B1** in `App._check_request_rules` (`backend/app.py`) and tested in `tests/backend/test_routes.py`
- request size 32 KiB: **enforced since B1** (`MAX_BODY_BYTES`, 413 `PAYLOAD_TOO_LARGE`, before any parsing)
- digest and certificate recomputation (Certificate verification is recomputed by the backend)

## Decision: the MVP has exactly 3 businesses

The guide says the MVP has "exactly 3 businesses" with an optional extension to "at most 5".
The schema now enforces **exactly 3**. Proposed by Developer 2. Developers 1 and 3 agreed in person on 2026-11-07
(reported by Developer 2; this was not recorded in writing by Developers 1 and 3 themselves).
Why: it matches the frozen MVP text, and widening later is backward compatible while tightening later is not.
With 3 businesses each deliverable has at most 3 possible providers, so the exhaustive search has at most
3^6 = 729 candidates. The guide's 5^6 = 15625 figure only applies to the 5-business extension.

Limits changed to 3: `Scenario.businesses` (min and max), `Draft.businesses`, `ExtractRequest.descriptions`
(min stays 1 so a partly filled input is allowed) and `Result.businessMetrics`.
The case "four businesses (MVP is exactly 3)" in `fixtures/contract-cases.json` pins it in both languages.

To enable the 5-business extension later, it is a joint contract change: raise those four maxItems values to 5
(and `Scenario.businesses` maxItems), add valid fixtures for 4 and 5, remove the four-businesses invalid case,
regenerate, rerun all tests, and update the UI layout and optimizer tests.
