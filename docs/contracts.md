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

- business ids unique; deliverable ids unique
- every `ownerId` references an existing business; every `Change.businessId` references one
- at most one estimate per service per business
- each owner has an estimate for its own required service, otherwise **422 `MISSING_BASELINE`**
- `SimulateRequest`: text together with non-empty `changes`, or text with `apply: true`, is rejected.
  The JSON Schema rejects it, but the generated Pydantic model cannot, so two parity cases are marked xfail
- request size 32 KiB (`MAX_BODY_BYTES` in `backend/app.py` is defined but not enforced yet)
- digest and certificate recomputation (Certificate verification is recomputed by the backend)

## Open contract question (needs a joint decision)

The guide says the MVP has "exactly 3 businesses" with an optional extension to "at most 5".
The schema currently allows **3 to 5**. If the MVP must reject 4 or 5, enforce it in a validator or
tighten `businesses.maxItems` in `scenario.schema.json` and regenerate.
