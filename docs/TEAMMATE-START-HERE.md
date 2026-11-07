# TradeMind: start here (Developers 1 and 3)

Repo: https://github.com/bchris2237/Hackathon11-7 (branch `main`). If you get a 403 or "permission denied",
ask `bchris2237` to add your GitHub account as a collaborator.

## 1. Install (once)

- Git
- Python **3.12** (python.org installer). Not 3.13 or newer.
- Node **24** (nodejs.org, LTS 24.x)

## 2. Clone and set your git identity

```powershell
git clone https://github.com/bchris2237/Hackathon11-7 trademind
cd trademind
git config user.name "Your Name"
git config user.email "your-github-email"
```

## 3. Install the project

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
npm --prefix frontend ci
npx --prefix frontend playwright install chromium
```

macOS/Linux:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
npm --prefix frontend ci
npx --prefix frontend playwright install chromium
```

## 4. Check it works

Terminal 1 (venv active): `python -m backend.local`
Terminal 2: `npm --prefix frontend run dev`

- Open http://127.0.0.1:8000/health. Expect `{"status": "ok", "schemaVersion": "1", "commit": "unknown", "mode": "fixture"}`.
- Open http://127.0.0.1:5173. Expect an amber **FIXTURE MODE** banner and "Backend OK".

Stop both with Ctrl+C, then run these (venv active):

```powershell
python -m pytest tests/optimizer tests/backend
npm --prefix frontend run typecheck
npm --prefix frontend test -- --run
npm --prefix frontend run build
```

Expected: pytest **63 passed, 2 xfailed**, Vitest **47 passed**, typecheck and build with no errors.
Tell Developer 2 the exact output if anything differs. Do not "fix" it by upgrading packages.

## 5. Review (about 15 minutes)

- Read `GENERAL-GUIDELINES.md`, `docs/contracts.md` and `docs/phase-0-checklist.md`.
- Tick your section of the review checklist in `docs/phase-0-checklist.md` locally, but do not commit those edits.
- Open `fixtures/canonical.json` and work out baseline 1440 and optimum production 540 by hand.
- Answer in the group chat: should the MVP allow only exactly 3 businesses, or 3 to 5? The schema currently allows 3 to 5.
- Reply "reviewed and reproduced" to Developer 2.

## 6. Create your branch (once, after Developer 2 confirms)

| | Developer 1 | Developer 3 |
|---|---|---|
| Branch | `git switch -c feature/optimizer` | `git switch -c feature/frontend` |
| You own | `optimizer/**`, `tests/optimizer/**` | `frontend/**`, `tests/e2e/**` |
| First prompt | A1 | C1 |

```powershell
git push -u origin feature/optimizer     # Developer 3: feature/frontend
```

## 7. Working rules

- Start each prompt with the short instruction from `GENERAL-GUIDELINES.md`, then paste the full prompt. Run one prompt at a time.
- Edit only the paths you own. Need a change elsewhere, especially in `contracts/`? Message Developer 2 with the file,
  the reason and an example. Never hand-edit `contracts/models.py` or `contracts/models.ts`.
- To commit: `git status`, then `git add <exact files>` (never `git add .`), then `git diff --staged --stat`,
  then `git commit -m "..."`, then `git push`.
- Never commit `.env`, AWS credentials or `node_modules`. Never paste secrets into the assistant.
- Never force-push. Never push to `main`; Developer 2 merges at checkpoints.
