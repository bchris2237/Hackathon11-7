# TradeMind

TradeMind models **labor-time exchanges** between small businesses under supplied estimates. Describe three businesses (what each
needs done, how long each service takes them), and it finds who should do which job so the group spends the least total time,
while no business ends up worse off. The numbers come from a deterministic optimizer; a language model only reads descriptions and
writes plain-language explanations, and everything it says is checked against recomputed numbers.

It is **not** a profit, pricing or quality tool. Currency is outside the model. Savings are minutes of effort.

## Canonical example (fictional)

DevCo needs design, DesignCo needs writing, CopyCo needs coding. Each takes 480 minutes for what it needs but only 180 for what
the others need. Alone: 1,440 minutes. Exchanging: 540 minutes. 900 minutes saved, 300 for each business.

## Live deployment (hackathon demo, fictional data only)

| Part | Where |
|---|---|
| Website (static HTML/JS/CSS, Amplify Hosting) | https://main.d2pd0bxuetqhvk.amplifyapp.com |
| API (API Gateway + Lambda, us-east-1) | https://kw3bawuyy8.execute-api.us-east-1.amazonaws.com (`GET /health`) |
| Language model | Amazon Nova 2 Lite on Amazon Bedrock |

The public API has **no authentication**. It is for fictional demo data only. See `docs/compliance/B8.md` for what protects it and what does not.
If the AWS account is torn down these addresses stop working.

## How it fits together

```
browser (frontend/, static site)
   |  JSON over HTTPS
API Gateway HTTP API  ->  Lambda (backend/app.py)
                              |-- optimizer/api.py     exact search, certificate, what-if   (pure Python, no AWS)
                              `-- Amazon Bedrock       extract facts from text, explain results (never chooses allocations)
```

| Folder | What | Owner |
|---|---|---|
| `optimizer/` | exact bounded solver, independent certificate, what-if engine | Developer 1 |
| `backend/`, `infra/`, `scripts/`, `contracts/`, `fixtures/` | API, validation, AWS template, deploy scripts, frozen v1 contract | Developer 2 |
| `frontend/`, `tests/e2e/` | the website | Developer 3 |
| `docs/` | contracts, deployment, costs, per-step compliance records, integration notes | everyone |

## Run it locally

```powershell
python -m pip install -e ".[dev]"
python -m backend.local                      # API on http://127.0.0.1:8000 (fixture mode)
npm --prefix frontend ci
npm --prefix frontend run dev                # http://127.0.0.1:5173
```

Checks:

```powershell
python -m pytest tests/optimizer tests/backend
python scripts/generate_contracts.py --check
npm --prefix frontend run typecheck
npm --prefix frontend test -- --run
npm --prefix frontend run build
npm --prefix frontend run e2e
```

## Deploy

1. Backend: `scripts\deploy.ps1` (see `docs/deployment.md`). It builds first and only deploys with `-Deploy` and a typed account id.
2. Website: `scripts\deploy_frontend.ps1 -AppId <amplify app id> -Region <region> -ApiUrl <api url>` builds in live mode, refuses to publish a
   bundle with credentials or without the API address, zips with forward-slash paths, deploys, and checks every asset over the internet.
3. The API's `AllowedOrigin` must be exactly the website address. Verify everything with `python scripts\smoke.py ... --frontend-url <site>`.

## Read next

* `docs/integration-notes.md`: how the three tracks were joined and what was fixed in the optimizer
* `docs/aws-costs.md`: measured and estimated monthly cost, plus the operations runbook
* `docs/contracts.md` and `GENERAL-GUIDELINES.md`: the frozen v1 contract
* `docs/compliance/`: per-step design notes, evidence, and what is still UNVERIFIED. Human understanding reviews are **PENDING** until each teammate confirms.
