# Ownership, handoff and branch instructions

Summary of `GENERAL-GUIDELINES.md` for the Phase 0 handoff. If they disagree, the guide wins.

## Who owns what

| Path | Before handoff | After the foundation is reviewed and committed |
|---|---|---|
| `contracts/**`, `fixtures/**`, root config (`pyproject.toml`, locks, `.gitignore`, `.env.example`), `scripts/**`, `backend/**`, `infra/**`, `tests/backend/**` | Developer 2 | Developer 2 (shared contract edits need a joint versioned decision) |
| `frontend/**` (shell, package config, lockfile), `tests/e2e/**` | Developer 2 created it | **Developer 3** |
| `optimizer/**`, `tests/optimizer/**` | Developer 2 created the placeholder | **Developer 1** |
| `docs/compliance/<prompt-id>.md` | | Each developer owns the record for their own prompts |

Handoff is not automatic. It happens when the team has done the review checklist in
`docs/phase-0-checklist.md` and Developer 2 has committed and pushed the foundation.

Need a change in someone else's files? Send a small request (affected file/interface, reason, desired
behavior, compatibility impact, acceptance example). Do not let an assistant edit their files.

## Branches

| Developer | Track | Branch | Prompts |
|---|---|---|---|
| 1 | Optimizer | `feature/optimizer` | A1 to A10 |
| 2 | Backend / cloud | `feature/backend` | B1 to B10 |
| 3 | Frontend | `feature/frontend` | C1 to C10 |

The foundation goes on `main` first. A branch is a version of the same workspace, never a second app.

### Developer 2: commit the foundation (not done yet)

Phase 0 deliberately did **not** commit anything. Review first, then stage exact files (no `git add .`):

```bash
git status
git add .env.example .gitattributes .gitignore .nvmrc .python-version GENERAL-GUIDELINES.md README.md \
  pyproject.toml requirements.lock backend contracts fixtures frontend optimizer scripts tests docs
git status          # confirm nothing unexpected is staged (no .venv, node_modules, .env, dist)
git diff --staged --stat
git commit -m "Phase 0: monorepo scaffold, contracts, fixtures, health endpoint, frontend shell"
git push origin main
git tag phase-0-foundation
git push origin phase-0-foundation
```

`projectfilemain` (an unrelated HTML calculator already on `main`) is not part of this commit and was
left untouched. Decide separately whether to keep it.

### Everyone else, then each developer

```bash
git clone <repo-url> trademind-ai   # Developers 1 and 3, first time
cd trademind-ai
git fetch origin
git switch main && git pull
git switch -c feature/optimizer      # Developer 1.  Developer 3: feature/frontend.  Developer 2: feature/backend
```

Create your branch once. Later, at a maintainer-announced checkpoint:

```bash
git status                  # must be clean, or deliberately stashed
git fetch origin
git switch feature/<yours>
git merge origin/main
```

After each prompt: inspect `git diff`, run your checks, stage exact owned files, inspect `git diff --staged`,
commit, push your branch. Never commit `.env`, credentials, `node_modules`, caches or logs with secrets.
Never force-push `main`. Resolve conflicts with the owner of the code, never by taking one whole side.
