# Compliance record: Phase 0 (foundation)

Author of the changes: Developer 2's role (initial scaffold), executed by the coding assistant.
Date: 2026-11-07. Commits: `35a95fe` (foundation) and `0905883` (exactly-3 contract change), tagged `phase-0-foundation` at `0905883`.

## Design explanation

One JSON Schema set is the source of truth. `scripts/generate_contracts.py` bundles it and generates Pydantic
models and TypeScript types, so the two languages cannot drift. Fixtures and a shared case file
(`fixtures/contract-cases.json`) are run by both the Python and TypeScript suites. The backend is a pure
`handle_request(settings, method, path, request_id)` function behind a thin standard-library server. Only
`GET /health` returns success. Every other contract route answers 503 `NOT_IMPLEMENTED`. `APP_MODE=live` refuses
to start. The frontend is a shell that labels fixture mode permanently and reports an unreachable backend as
unavailable. The optimizer is a placeholder that raises.

## Important functions and files

| Where | What it does |
|---|---|
| `scripts/generate_contracts.py: bundle(), generate_python()` | Merges the 3 schemas into one document, runs the generators; `--check` detects stale output |
| `backend/config.py: load_settings()` | Reads env names from `.env.example`; rejects `live`, bad modes, bad ports, wildcard CORS |
| `backend/app.py: handle_request()` | Routing, `/health` built through the generated `HealthResponse`, honest 503/404/405 errors via `ErrorEnvelope` |
| `backend/local.py: make_handler()` | HTTP wrapper: CORS allow-list echo, request ids, bounded body drain, mode header |
| `frontend/src/api.ts: fetchHealth()` | Validates the `/health` shape; throws plain "unavailable" messages |
| `frontend/src/App.tsx` | Mode banner, abortable health check (stale responses cannot overwrite newer state) |
| `tests/backend/test_contracts_parity.py` | Schema, Pydantic, fixture and generated-file checks |
| `frontend/src/contracts.test.ts` | Same cases through Ajv |

## Tests actually run

See `docs/phase-0-checklist.md`: pytest 64 passed and 2 xfailed; Vitest 48 passed; `tsc --noEmit` clean;
Vite build ok; Playwright 2 passed; `generate_contracts.py --check` up to date; `npm audit` 0 vulnerabilities;
lock install into a fresh venv verified.
**NOT RUN:** Python dependency audit; OpenAPI linter; non-Windows platforms; fresh clone of a committed checkout.

## Capability matrix

Status: IMPLEMENTED, SIMULATED or INCOMPLETE. Deployment: VERIFIED, UNVERIFIED or N/A.

| Capability | Status | Deployment |
|---|---|---|
| JSON Schemas, OpenAPI route file | IMPLEMENTED | N/A |
| Generated `models.py` / `models.ts` + staleness check | IMPLEMENTED | N/A |
| Canonical, coordination and disruption fixtures | IMPLEMENTED | N/A |
| Extraction fixture (hand-written, labelled `mode: fixture`) | **SIMULATED** | N/A |
| Local `GET /health` | IMPLEMENTED | UNVERIFIED (not deployed) |
| All routes except `/health` (`/extract`, `/confirm`, `/optimize`, `/simulate`, `/explain`, `/certificates`, `/profiles/{id}`) | INCOMPLETE (answer 503 `NOT_IMPLEMENTED`) | N/A |
| `APP_MODE=live` startup refusal | IMPLEMENTED | N/A |
| Frontend shell (banner, health display) | IMPLEMENTED (shell only) | UNVERIFIED (no Amplify) |
| Frontend product screens (input, confirm, graph, certificate, what-if) | INCOMPLETE | N/A |
| Optimizer, verification, certificates | INCOMPLETE (placeholder raises) | N/A |
| Bedrock extraction / interpretation / explanation | INCOMPLETE | UNVERIFIED |
| Runtime semantic validators (unique ids, references, `MISSING_BASELINE`, body size) | INCOMPLETE | N/A |
| SAM template, Lambda, API Gateway, Amplify, IAM, Budgets | INCOMPLETE | UNVERIFIED |
| Optional DynamoDB / S3 profiles and reports | INCOMPLETE | UNVERIFIED |

## Limitations

See "Pending issues and limitations" in `docs/phase-0-checklist.md`. Short version: the foundation is pushed and tagged,
only `/health` worked in Phase 0 (B1 later added the full router), nothing touches AWS, and several runtime validations are deferred to the B and A tracks.

## Judge questions with grounded answers

1. **Why JSON Schema as the source and generated types, rather than writing Pydantic and TypeScript by hand?**
   Two hand-written copies drift, and the first mismatch shows up as a runtime failure in a demo. Here one file is
   edited, `generate_contracts.py --check` and a pytest fail if either generated file is stale, and the same
   `contract-cases.json` runs through JSON Schema, Pydantic and Ajv. We also found the limits: Pydantic cannot express
   the schema's `if/then` rule, so two cases are marked xfail and the backend must enforce that rule.
2. **Why does `APP_MODE=live` refuse to start instead of falling back to fixtures?**
   A silent fallback would let a deployed demo show simulated numbers as real. Startup raises `ConfigError` and exits
   with code 2 (tested). Fixture mode is the only mode, and it is shown as a banner in the UI and an
   `X-TradeMind-Mode` header on responses.
3. **Why integer minutes and strict Pydantic types?**
   Rounding hours to floats before the capacity and savings checks could flip a feasibility result. Strict types make
   `"600"` and `600.5` fail instead of being coerced, and the 1000000 bound keeps all arithmetic exact.
   The tests include both inputs.
4. **What does Phase 0 prove and what does it not?**
   It proves the toolchain installs from locks, the contracts and fixtures agree across Python and TypeScript, and a
   browser can reach a healthy local backend. It does not prove any optimization, any AWS integration, or any
   deployment. Those are all INCOMPLETE or UNVERIFIED in the matrix above.
5. **What was the one real bug the checks found?**
   The browser page ran on `127.0.0.1:5173` but CORS allowed only `localhost:5173`, so `/health` was blocked and the
   UI said "unavailable". Playwright found it; the unit tests could not. It is now an allow-list with a test that an
   unlisted origin gets no CORS header.

## Human understanding status

| Developer | Status |
|---|---|
| Developer 1 | Approved (confirmed by the team, relayed by Aidan Cheng, 2026-11-07) |
| Developer 2 | Approved (confirmed by the team, relayed by Aidan Cheng, 2026-11-07) |
| Developer 3 | Approved (confirmed by the team, relayed by Aidan Cheng, 2026-11-07) |
