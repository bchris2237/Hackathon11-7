# TradeMind

Small businesses can often save time by doing the tasks they are fast at for each other. TradeMind finds the best swap.

You describe three businesses: what each one needs done, how long each service takes them, how much spare time they have, and what
it costs them to coordinate. TradeMind finds who should do which job so the group spends the least total time, with no business
over its capacity and none worse off. The numbers come from a deterministic optimizer. A language model only reads descriptions and
writes plain-language explanations, and everything it says is checked against recomputed numbers.

It is **not** a profit, pricing or quality tool. Currency is outside the model. Savings are minutes of effort.

## Canonical example (fictional)

DevCo needs design, DesignCo needs writing, CopyCo needs coding. Each takes 480 minutes (8 h) for what it needs but only 180 minutes
(3 h) for what the others need.

* Alone: 3 x 8 h = **1,440 minutes (24 h)**.
* Exchanging (DevCo codes, DesignCo designs, CopyCo writes): 3 x 3 h = **540 minutes (9 h)**.
* Saved: 900 minutes, 300 for each business.

On the website, press **Load fictional sample**, **Extract draft**, **Confirm scenario**, then **Optimize**.

## How a person uses it

1. **Describe.** Type the numbers, or paste a paragraph for each business and press Extract draft. The model drafts the numbers. It
   never guesses a missing one: gaps are shown in red.
2. **Review and confirm.** A person checks the draft and fills the gaps. Extract keeps what you typed unless you changed the description.
3. **Optimize.** Plain code computes the best plan and shows a table, a graph of who works for whom, savings, and a certificate that a
   separate calculation re-checks.
4. **What-if and explain.** Change an input (for example a business loses capacity) and compare. The model can explain a result in plain
   language, but the figures are authoritative.

Design rule: the model never chooses an allocation or a number. A wrong model answer can give a wrong draft that a person catches at
review. It cannot give a wrong plan.

## Live deployment (hackathon demo, fictional data only)

| Part | Where |
|---|---|
| Website (static HTML/JS/CSS, Amplify Hosting) | https://main.d2pd0bxuetqhvk.amplifyapp.com |
| API (API Gateway + Lambda, us-east-1) | https://kw3bawuyy8.execute-api.us-east-1.amazonaws.com (`GET /health`) |
| Language model | Amazon Nova 2 Lite on Amazon Bedrock |

The public API has **no authentication**. It is for fictional demo data only. See `docs/compliance/B8.md` for what protects it and what does not.
If the AWS account is torn down these addresses stop working.

## AWS services and why

| Service | Used for | Why |
|---|---|---|
| Amazon Bedrock (Nova 2 Lite) | Reads descriptions into a draft, writes explanations | Managed model with no hosting and no API keys, access controlled by IAM. Claude was blocked by an account Marketplace permission, so Nova is used. |
| AWS Lambda | Runs the backend and the Python optimizer | Tiny, bursty workload: pay per call, no servers. |
| Amazon API Gateway (HTTP API) | Public HTTPS entry point | Standard front door for Lambda, with HTTPS, throttling and CORS. |
| AWS Amplify Hosting | Serves the static website | Simplest static hosting with HTTPS. Builds are uploaded by script. |
| AWS IAM | Permissions | The Lambda role may only invoke the one Bedrock model. No stored keys. |
| AWS CloudFormation / SAM | Defines and deploys the backend | One command to deploy or tear down. |
| Amazon CloudWatch Logs | Logs and usage | Comes with Lambda. Only allow-listed fields are logged, secrets are redacted. |
| Amazon S3 | Holds the SAM deployment package | Required by SAM. Stores no app data. |

There is **no database and no user accounts**. Nothing a user enters is stored.

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
| `frontend/`, `tests/e2e/` | the website (React, TypeScript, Vite, React Flow) | Developer 3 |
| `docs/` | contracts, deployment, costs, per-step compliance records, integration notes | everyone |

Endpoints: `GET /health`, `POST /extract`, `/confirm`, `/optimize`, `/simulate`, `/explain`.

## Does it give the right numbers?

* `python scripts/accuracy_check.py --local 3000` compares the optimizer with a separate brute-force search on random scenarios
  (3 to 5 businesses) and checks that every figure adds up. Last run: 3,000 cases, 0 failures.
* The same script against the live API (`--live 150 --api-url ... --origin ...`): 150 cases, 0 failures.
* Last full runs: 1,211 backend and optimizer tests, 338 frontend unit tests, 68 browser end-to-end tests, 35 live smoke checks.

This shows agreement on those cases. It is not a proof for every input.

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
python scripts/accuracy_check.py --local 3000
npm --prefix frontend run typecheck
npm --prefix frontend test -- --run
npm --prefix frontend run build
npm --prefix frontend run e2e
```

If `VITE_DATA_MODE` or `VITE_API_BASE_URL` are set in your shell (the frontend deploy script can leave them set), frontend tests may fail
because they expect fixture mode. Clear them first.

## Deploy

1. Backend: `scripts\deploy.ps1` (see `docs/deployment.md`). It builds first and only deploys with `-Deploy` and a typed account id.
2. Website: `scripts\deploy_frontend.ps1 -AppId <amplify app id> -Region <region> -ApiUrl <api url>` builds in live mode, refuses to publish a
   bundle with credentials or without the API address, zips with forward-slash paths, deploys, and checks every asset over the internet.
3. The API's `AllowedOrigin` must be exactly the website address. Verify everything with `python scripts\smoke.py ... --frontend-url <site>`.

## Limits

* Extraction on Nova is weaker than a larger model on messy text. Details it misses appear as red fields to fill in by hand.
* Three services (coding, design, writing) and three businesses on the live site. The optimizer can handle up to five in extended mode.
* Figures are modeled labor time under the supplied estimates, not profit or revenue.
* The idea builds on well-known ones (comparative advantage, time banks, task assignment). The contribution is a checkable, AI-assisted workflow.
* Public site, no login: protected by an origin check and rate limits only.

## AI assistance

Built with Kiro under the developers' supervision. Per-step records in `docs/compliance/` say what was generated and what is still
UNVERIFIED. Human understanding reviews were approved by the team (relayed by Aidan Cheng, 2026-11-07).

## Read next

* `docs/integration-notes.md`: how the three tracks were joined and what was fixed in the optimizer
* `docs/aws-costs.md`: measured and estimated monthly cost, plus the operations runbook
* `docs/contracts.md` and `GENERAL-GUIDELINES.md`: the frozen v1 contract
* `docs/compliance/`: per-step design notes, evidence, and what is still UNVERIFIED
