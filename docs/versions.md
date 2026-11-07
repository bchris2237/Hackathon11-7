# Pinned versions (frozen at Phase 0)

Recorded 2026-11-07 from the PyPI and npm registries and from the installed environment. Direct
dependencies are pinned exactly in `pyproject.toml` and `frontend/package.json`. The full resolved
trees are in `requirements.lock` (universal, with SHA-256 hashes) and `frontend/package-lock.json`.
**Do not upgrade individually.** Upgrading is a team decision: change the pin, re-lock, rerun every
check in `docs/phase-0-checklist.md`, then record it here.

## Runtimes

| Tool | Version | Why |
|---|---|---|
| Python | 3.12 (verified on 3.12.10, python.org build) | Frozen contract and Lambda runtime. `requires-python = ">=3.12,<3.13"` |
| Node.js | 24.x (verified on 24.22.2, an LTS line) | `engines` in `frontend/package.json` is `>=24 <25`. Vite 8, Vitest 5 and jsdom 31 all support it |
| npm | 11.19.1 (bundled with Node 24.22.2) | |
| uv | 0.13.7 | Only needed to regenerate `requirements.lock`; not needed to install |

## Python (direct)

| Package | Version | Role |
|---|---|---|
| pydantic | 2.14.3 | Models, strict validation |
| boto3 | 1.43.126 | AWS SDK (installed now, unused until B-track) |
| pytest | 9.2.0 | Tests |
| hypothesis | 6.170.15 | Property tests (A-track) |
| jsonschema | 4.26.0 | Schema validation in tests |
| pyyaml | 6.0.3 | Parses `api.openapi.yaml` in tests |
| datamodel-code-generator | 0.91.0 | JSON Schema to Pydantic |
| setuptools (build only) | 84.2.1 | Editable install |

## Frontend (direct)

| Package | Version |
|---|---|
| react, react-dom | 19.3.1 |
| @xyflow/react | 12.12.1 (installed for C-track, not used yet) |
| vite | 8.3.6 |
| @vitejs/plugin-react | 6.2.1 (peer: vite ^8) |
| typescript | 7.0.2 |
| tailwindcss, @tailwindcss/vite | 4.3.5 (plugin peer: vite ^8 ok) |
| vitest | 5.1.0 (peer: vite ^8 ok) |
| jsdom | 31.0.0 |
| @testing-library/react / dom / jest-dom / user-event | 16.3.3 / 10.4.2 / 7.0.1 / 14.6.8 |
| @playwright/test | 1.65.0 (Chromium installed with `npx playwright install chromium`) |
| ajv | 8.20.0 |
| json-schema-to-typescript | 16.0.0 |
| @types/react, @types/react-dom | 19.3.0 |
| @types/node | 24.19.2 (latest 24.x, matches Node 24) |

## How versions were chosen

Latest stable release of each package at the time, then peer-dependency ranges were checked in the
registry (for example `@vitejs/plugin-react` requires vite ^8, `vitest` accepts vite ^8 and Node 24).
`npm ci`, typecheck, unit tests, build and e2e all ran on this set (see the checklist).
`@types/node` is held to the 24.x line to match the Node runtime rather than the newest 26.x.

## Known version notes

- `datamodel-code-generator` 0.91.0 prints a FutureWarning that black/isort will become optional extras.
  Harmless with the pin; revisit when upgrading it.
- TypeScript 7 and Vite 8 are recent major versions. They worked for everything in Phase 0.
- Teammates on Node 26 will see an npm engine warning (`>=24 <25`). Use Node 24 to match the lock.

## Re-locking (maintainer only)

```bash
uv pip compile pyproject.toml --extra dev --python-version 3.12 --universal --generate-hashes --no-header -o requirements.lock
npm --prefix frontend install        # then commit package-lock.json
```
