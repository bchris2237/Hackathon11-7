# Phase 0 checklist

Phase 0 is shared setup. It is **not** a cloud deployment and not one of the 30 feature prompts.
Results below were observed on **2026-11-07 14:13 to 14:14 (UTC-08:00)**, Windows 11, PowerShell.

## Status at a glance

| Item | Status |
|---|---|
| Foundation committed and pushed | **NOT DONE.** Intentionally left for Developer 2 after review. Base commit is `2ae32ea` on `main`; every Phase 0 file is untracked |
| Local checks (below) | All passed on the author's machine |
| Reproduced from a fresh checkout by teammates | **PENDING** (needs the commit first, then each developer) |
| AWS / Bedrock / SAM / Amplify | **N/A for Phase 0.** Nothing deployed, no credentials used |
| Optimizer, extraction, simulation, certificates, real UI | **INCOMPLETE** by design (A/B/C prompts) |
| Human understanding reviews | **PENDING** for all three developers |

## Tool versions used

Python 3.12.10 (python.org build), Node 24.22.2, npm 11.19.1, git 2.56.0.windows.2. Full dependency table: `docs/versions.md`.

## Install and start from scratch

Prerequisites: Git, **Python 3.12** and **Node 24**. Use the python.org installer or your usual version manager.

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
npm --prefix frontend ci
npx --prefix frontend playwright install chromium     # only needed for e2e
```

### macOS / Linux

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
npm --prefix frontend ci
```

Exact, hash-verified Python install (what CI should use):
`python -m pip install --require-hashes --no-deps -r requirements.lock` then `python -m pip install --no-deps -e .`

### Start (two terminals, venv active in the first)

| What | Command | URL |
|---|---|---|
| Backend (fixture mode) | `python -m backend.local` | http://127.0.0.1:8000 |
| **Health check** | | **http://127.0.0.1:8000/health** |
| Frontend | `npm --prefix frontend run dev` | http://127.0.0.1:5173 |

Expected `/health` body: `{"status": "ok", "schemaVersion": "1", "commit": "unknown", "mode": "fixture"}`.
The page shows an amber **FIXTURE MODE** banner and "Backend OK". With the backend stopped, it shows
"Backend unavailable" instead of any invented status.

No `.env` file is needed for Phase 0. Defaults match `.env.example`. Set variables in the shell
(for example `$env:APP_MODE="fixture"`). The backend does not read `.env` files.

## Required commands and observed results

| # | Command (from repo root) | Observed result |
|---|---|---|
| 1 | `python -m pip install -e '.[dev]'` | exit 0 |
| 2 | `python -m pytest tests/optimizer tests/backend` | **64 passed, 2 xfailed**, exit 0 |
| 3 | `npm --prefix frontend ci` | 150 packages, exit 0 |
| 4 | `npm --prefix frontend run typecheck` | exit 0, no errors |
| 5 | `npm --prefix frontend test -- --run` | **2 files, 48 tests passed**, exit 0 |
| 6 | `npm --prefix frontend run build` | built, exit 0 (`tsc --noEmit` then `vite build`) |
| 7 | `python scripts/generate_contracts.py --check` | "Generated contract files are up to date: models.py, models.ts" |
| 8 | `npm --prefix frontend run e2e` (Playwright, Chromium) | **2 passed**, against the real backend and dev server |
| 9 | `npm --prefix frontend audit` | 0 vulnerabilities |
| 10 | Fresh venv from `requirements.lock` with `--require-hashes`, then pytest | 63 passed, 2 xfailed (run before the exactly-3 change, not repeated) |

Notes on these results:

- **Rerun after the exactly-3 schema change:** commands 2, 4, 5, 6 and 7 (regenerate, check, pytest, typecheck, Vitest, build all
  passed). Commands 3, 8, 9 and 10 (`npm ci`, Playwright e2e, audit, lock install) were **not rerun**. The change touched only
  schema limits, docs and one test case, but treat those four rows as results from before it.
- **The 2 xfails are intentional.** They are the two `SimulateRequest` conditional rules ("text requires
  no changes and `apply: false`"). The JSON Schema rejects them and the test asserts that; the generated
  Pydantic model cannot express them, so the backend validator (B-track) must. See `docs/contracts.md`.
- Step 8 also caught a real bug during setup: the backend allowed only `http://localhost:5173` while the
  page was served from `http://127.0.0.1:5173`, so the browser blocked `/health`. `ALLOWED_ORIGIN` is now a
  comma-separated allow-list and unlisted origins get no CORS header (tested).
- **NOT RUN:** Python dependency audit (`pip-audit` is not installed); a dedicated OpenAPI linter (the
  YAML is parsed and every `$ref` is resolved by a test, nothing more); e2e on macOS/Linux or Firefox/WebKit;
  a clone of a committed checkout (nothing is committed).

## Schema parity output

`tests/backend/test_contracts_parity.py` (all in the 64 passed):

- all 3 schemas are valid JSON Schema 2020-12; the OpenAPI file parses and every `$ref` resolves
- `canonical.json` and `coordination.json` validate in JSON Schema **and** parse in Pydantic
- `extraction.json` request and response validate; response `mode` is `fixture`
- both disruption patches (`noExchange`, `infeasible`) validate as `Change` objects
- **21 invalid scenarios**, 6 invalid and 3 valid simulate requests: rejected or accepted identically by JSON Schema and Pydantic
  (except the 2 xfails above)
- `frontend/src/contracts.test.ts` runs the **same case file** through Ajv: all 48 tests pass
- `models.py` is not stale (test), and `generate_contracts.py --check` confirms `models.ts` is not stale

## Fixture validation output

Computed from fixture inputs only. This is **not** the optimizer and it does not check any solver output:

| Check | Observed |
|---|---|
| Baseline per business (devco / designco / copyco) | 480 / 480 / 480, total **1440** minutes |
| Capacity and coordination in `canonical.json` | 600 and 0 for every business |
| Deliverables | `d-design` (devco), `d-writing` (designco), `d-coding` (copyco) |
| `coordination.json` vs `canonical.json` | identical except coordination = 60 for every business |
| Ids unique; every `ownerId` exists; one estimate per service; every owner has its baseline estimate | all true |
| `extraction.json` provenance quotes | every quote is found verbatim in its description; `copyco` coordination stays `null` and is listed in `missingFields` |

The expected optimizer results from the guide (540 production, 900 savings, 720 net with coordination, `no_exchange`
for the DesignCo 600-minute disruption, `infeasible` with all capacities 0) are **not** asserted anywhere yet.
Those are Developer 1's oracle tests (A-track). The numbers were not hardcoded as solver output.

## What was verified by hand against the running backend

| Probe | Observed |
|---|---|
| `GET /health` | 200, contract-shaped body; headers `X-TradeMind-Mode: fixture`, `X-Request-Id` |
| `POST /optimize` | 503 `NOT_IMPLEMENTED` in an error envelope with `requestId` (also unit-tested for all other routes) |
| `GET /nope` | 404 |
| `APP_MODE=live python -m backend.local` | refuses to start, exit code 2, message explains the adapters do not exist |
| `Origin: https://evil.example` | no `Access-Control-Allow-Origin` header |

## Files created (what each is for)

See the summary in the Phase 0 report. Quick map:

- Root config: `pyproject.toml`, `requirements.lock`, `.gitignore`, `.gitattributes`, `.env.example`, `.python-version`, `.nvmrc`, `README.md`
- Contract: `contracts/*.schema.json`, `api.openapi.yaml`, generated `models.py` / `models.ts`
- Fixtures: `fixtures/canonical.json`, `coordination.json`, `disruption.json`, `extraction.json`, `contract-cases.json`
- Backend: `backend/config.py`, `app.py`, `local.py`
- Placeholder: `optimizer/__init__.py` (raises `OptimizerNotImplementedError`)
- Frontend: `frontend/` (Vite + React + TS + Tailwind shell)
- Tools/tests: `scripts/generate_contracts.py`, `tests/backend/*`, `tests/optimizer/test_placeholder.py`, `tests/e2e/*`
- Docs: `docs/versions.md`, `docs/contracts.md`, `docs/ownership-and-branches.md`, `docs/compliance/phase-0.md`, this file

## Review checklist (all three developers, before the handoff)

Tick only after you have actually done it. Leave unchecked otherwise.

**Everyone**
- [ ] Read `GENERAL-GUIDELINES.md` and `docs/contracts.md`
- [ ] Open `fixtures/canonical.json` and recompute baseline 1440 and optimum production 540 by hand
- [ ] Confirm the decision in `docs/contracts.md`: the schema now requires exactly 3 businesses (proposed by Developer 2)
- [ ] Agree that `contracts/models.*` are never hand-edited
- [ ] Reproduce startup from a fresh clone of the committed foundation (install, both start commands, `/health`, page)
- [ ] Run commands 2 to 6 above and compare with the table

**Developer 1 (optimizer)**
- [ ] Confirm minutes, baseline, coordination-once-per-participant and net-savings definitions match `result.schema.json`
- [ ] Confirm `Result` rules: `infeasible` has null metrics, totals and certificate; `no_exchange` is a feasible outcome
- [ ] Confirm the tie-break order and ratio rule can be implemented with integers only
- [ ] Accept ownership of `optimizer/**` and `tests/optimizer/**` (replace `test_placeholder.py` in A1)

**Developer 2 (backend / contracts)**
- [ ] Review `backend/app.py`, `config.py`, `local.py`: `live` refusal, 503 `NOT_IMPLEMENTED`, CORS allow-list
- [ ] Confirm the pending validator list in `docs/contracts.md` is the B-track's responsibility
- [ ] Commit and push the foundation (commands in `docs/ownership-and-branches.md`), tag `phase-0-foundation`
- [ ] Confirm no secrets or build output are staged (`git diff --staged --stat`)

**Developer 3 (frontend)**
- [ ] Review the shell: persistent mode banner, honest "Backend unavailable", abortable request
- [ ] Confirm `VITE_API_BASE_URL` and `VITE_DATA_MODE` are the only frontend variables
- [ ] Confirm importing types from `../../contracts/models` is acceptable (relative import, no alias)
- [ ] Accept ownership of `frontend/**` and `tests/e2e/**`

## Pending issues and limitations

1. Foundation is uncommitted (Developer 2 action).
2. Business count: the schema now enforces exactly 3 (changed after the first push; see `docs/contracts.md`).
   Decided: Developers 1 and 3 agreed in person on 2026-11-07 (reported by Developer 2). The first push (`35a95fe`)
   allowed 3 to 5, so anyone who cloned before the next push must pull.
3. Runtime validators missing by design: unique ids, `ownerId` references, unique service per business,
   `MISSING_BASELINE` (422), text-plus-changes rule, 32 KiB body limit (constant exists, not enforced).
4. `APP_MODE=live` always refuses to start. There is no Lambda handler, `infra/template.yaml`, or deploy script yet.
5. The frontend and backend never exchange anything except `/health`. Every other route is 503.
6. TypeScript fixtures are checked at runtime with Ajv. JSON imports widen enum values to `string`, so there is no
   compile-time check of fixtures against `models.ts`.
7. Windows note: the uv-managed standalone Python's `_ssl` module is blocked by this machine's Application Control
   policy, so pip could not reach HTTPS. The python.org build works. Teammates on a locked-down Windows machine
   may see the same; use the python.org installer.
8. `playwright.config.ts` starts the backend with the plain `python` command, so the venv must be active for `npm run e2e`.
9. `datamodel-code-generator` prints a FutureWarning about black/isort becoming optional (harmless while pinned).
10. `boto3` and `@xyflow/react` are installed but unused until B- and C-tracks.
11. Python dependency vulnerability scan: NOT RUN.

## Readiness for A1 / B1 / C1

| Track | Can start when | Starting point | Blocker today |
|---|---|---|---|
| **A1** (Developer 1) | Foundation is pushed and Developer 1 has branched `feature/optimizer` | `contracts/models.py`, `fixtures/*.json`, `optimizer/__init__.py` placeholder, `tests/optimizer/` | Foundation not yet committed |
| **B1** (Developer 2) | Immediately after committing; branch `feature/backend` | `backend/app.py` request handler, `config.py`, `contracts/models.py`, `tests/backend/` | Commit and push the foundation |
| **C1** (Developer 3) | Foundation is pushed and Developer 3 has branched `feature/frontend` | `frontend/src/`, `contracts/models.ts`, `fixtures/*.json`, `tests/e2e/` | Foundation not yet committed |

**Verdict:** the code is ready for A1, B1 and C1 on the author's machine. The team gate is **not open** until the
foundation is committed, pushed, tagged and reproduced by Developers 1 and 3 from a fresh clone.

## Understanding reviews (human confirmation)

| Developer | Walkthrough | Status |
|---|---|---|
| Developer 1 | Explains the mathematical example (fixtures baseline 1440) in their own words | **PENDING** |
| Developer 2 | Traces `GET /health` from request to response and explains why `live` is refused | **PENDING** |
| Developer 3 | Traces the shell from env config through the health request to what is rendered | **PENDING** |
| All | Each repeats the complete flow in their own words | **PENDING** |

Nobody has confirmed any of these. Mark each as done, with a date and name, only when it happens.

## Deviations from the guide (and why)

- Added `contracts/api.schema.json`. The guide lists three schema files, but request/response shapes need one canonical home.
- Backend is the Python standard library (`http.server`) for Phase 0. The guide fixes no web framework, and `/health` needs none.
- `ALLOWED_ORIGIN` accepts a comma-separated list of exact origins (no `*`) because of the 127.0.0.1 vs localhost bug above.
- `contracts` is an importable Python package so `from contracts.models import ...` works after the editable install.
