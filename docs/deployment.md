# Deploying TradeMind to AWS (B6)

**Status of this document: written and checked locally. Nothing described here has been run against a real AWS account by the
author of this file. Every cloud step below is UNVERIFIED until a human runs it and pastes the evidence into
`docs/compliance/B6.md`.**

What gets created: one Lambda function (Python 3.12), one HTTP API, one execution role, one CloudWatch log group, and the
bucket SAM uses to upload the code. Optional later: Amplify Hosting for the frontend. Nothing else. No database, no S3 data,
no authentication (public demo with fictional data only).

## 0. What is automated and what needs a human

| Step | Who | Why |
|---|---|---|
| Create or choose the AWS account, region and sign-in | **Human** | The project never assumes an account, region or credentials |
| Enable **Model access** for one Bedrock model and note its id | **Human** (console) | Access can need approval; the project never guesses a model id |
| Set a **Budget** alert | **Human** (console) | Budgets warn, they do not stop spending |
| Connect Amplify to the Git repository | **Human** (console OAuth) | Needs your GitHub authorization |
| Build and validate the template | `scripts\deploy.ps1` | No AWS calls |
| Create the stack | `scripts\deploy.ps1 -Deploy` after you type the account id | |
| Verify the whole live workflow | `scripts\smoke.py` | Real calls, small cost |

## 1. Prerequisites (once per machine)

- Windows PowerShell 5.1 or PowerShell 7, Git, Python 3.12 (see `docs/TEAMMATE-START-HERE.md`).
- **AWS CLI v2**: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html
- **AWS SAM CLI**: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html
  (`sam --version`). Docker is **not** needed.
- Sign in to AWS the normal way, for example `aws configure sso` then `aws sso login --profile <profile>`.
  **Never paste access keys into a chat, into this repository, into Amplify variables, or into the frontend.** Check who you are with
  `aws sts get-caller-identity` (it prints the account id and a user/role ARN, no secrets).
- The optimizer must be merged: `optimizer\api.py` must exist (Developer 1, prompts A1 to A6). Otherwise the deployed
  `/optimize`, `/simulate` and `/explain` answer 503. For an early "thin slice" you may pass `-AllowMissingOptimizer`.

## 2. Human console steps

### 2a. Choose the region and enable model access

1. In the AWS console pick the **region** where you want to run. **Bedrock is called in the same region as the stack**
   (Lambda sets `AWS_REGION` itself and it cannot be overridden).
2. Open **Amazon Bedrock → Model access** and request/enable one model. Wait until it shows as available.
3. Write down (do not guess):
   - the **model id** or **inference profile id** you will call, for example from the model's page or the **Inference profiles** page;
   - the **ARNs** the function needs permission for:
     - a plain foundation model: its `arn:aws:bedrock:<region>::foundation-model/<model-id>`;
     - an **inference profile**: the profile ARN `arn:aws:bedrock:<region>:<your-account-id>:inference-profile/<profile-id>`
       **and** the foundation-model ARN in **every region the profile routes to** (AWS documents this; leaving the second part out is the usual cause of
       a confusing `AccessDeniedException`). See "Prerequisites for inference profiles" in the Bedrock user guide.
4. The permission granted is only `bedrock:InvokeModel` (that is what `Converse` requires), on exactly those ARNs. Wildcards are rejected by the template.

### 2b. Budget alert

**Billing and Cost Management → Budgets → Create budget.** Pick a small monthly amount and email alerts. A budget is a warning, not a limit.
The hard limits in this stack are the reserved concurrency, the API throttles, the 32 KiB request cap and the per-call token cap.

### 2c. Amplify Hosting (the frontend)

Do this **before** deploying the backend, because the backend needs the frontend's origin for CORS.

1. **AWS Amplify → Create new app → GitHub →** authorize → pick `bchris2237/Hackathon11-7` and the branch you want hosted.
2. Tick **My app is a monorepo** and enter `frontend`. (Amplify then sets `AMPLIFY_MONOREPO_APP_ROOT=frontend`. The
   whole repository is checked out, so the frontend's import of `../../contracts/models` works.)
3. Build settings, shown here for Developer 3 to place in `amplify.yml` at the repo root (a file in the repo overrides console settings) **or** to paste in the console.
   This is a request to Developer 3, who owns `frontend/**`; nothing in this repo has created it yet:
   ```yaml
   version: 1
   applications:
     - appRoot: frontend
       frontend:
         phases:
           preBuild:
             commands:
               - npm ci
           build:
             commands:
               - npm run build
         artifacts:
           baseDirectory: dist
           files:
             - '**/*'
         cache:
           paths:
             - node_modules/**/*
   ```
4. **Environment variables.** Add exactly two, both public values, and nothing else:
   - `VITE_DATA_MODE` = `live`
   - `VITE_API_BASE_URL` = the stack output `ApiUrl` (you only know it after step 3; set it then and redeploy the frontend)
   Never add AWS keys here: Vite copies `VITE_*` values into the public JavaScript.
5. **Rewrites and redirects → Manage redirects.** Add a **Rewrite (200)** so every path that is not a static file is served by `index.html` (single page app).
   Amplify's own docs describe "Rewrite (200)" as the way to send all SPA traffic to `index.html`. The smoke test's deep-link check proves it works.
6. Note the app's URL, `https://<branch>.<appid>.amplifyapp.com`. That exact string is your **AllowedOrigin** (the stack parameter, which the template passes to the
   Lambda as the `ALLOWED_ORIGIN` environment variable; the backend then allows that one origin and no other).

## 3. Build (no AWS calls)

```powershell
cd <repo root>
sam validate --lint -t infra\template.yaml --region <region>
.\scripts\deploy.ps1                 # stages the package, runs sam validate --lint and sam build; deploys nothing
```
The script refuses to build a stack that has no optimizer unless you add `-AllowMissingOptimizer`.
Equivalent manual commands: stage as the script does, then `sam build -t infra\template.yaml`.

## 4. Deploy (creates billable resources)

```powershell
.\scripts\deploy.ps1 -Deploy `
  -Region <region> `
  -AllowedOrigin https://<branch>.<appid>.amplifyapp.com `
  -BedrockModelId <model-or-inference-profile-id> `
  -BedrockResourceArns "<arn1>,<arn2>"
```
What happens: it prints the AWS account and region, and you must **type the account id** to continue. `sam deploy` then shows the
CloudFormation change set and asks again before executing it. The commit id of `HEAD` is passed in and later returned by `GET /health`.

Reserved concurrency defaults to **5** (a hard ceiling on parallel model calls). A **new AWS account may have a concurrency limit too low to reserve any**;
CloudFormation then fails with a clear message. In that case, and only then, add `-ReservedConcurrency -1`. That removes the ceiling, so keep the budget alert and
the API throttles, and record the decision in `docs/compliance/B6.md`.

Equivalent manual commands (what the script runs):
```powershell
sam build -t infra\template.yaml
sam deploy -t .aws-sam\build\template.yaml --stack-name trademind-api --region <region> --resolve-s3 `
  --capabilities CAPABILITY_IAM --confirm-changeset --no-fail-on-empty-changeset `
  --parameter-overrides AllowedOrigin=... BedrockModelId=... BedrockResourceArns=... CommitSha=<sha> ReservedConcurrency=5
```

### Describe the stack

```powershell
aws cloudformation describe-stacks --stack-name trademind-api --region <region> `
  --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table
aws cloudformation describe-stack-events --stack-name trademind-api --region <region> --max-items 20
```
Outputs: `ApiUrl`, `HealthUrl`, `FunctionName`, `LogGroupName`, `BedrockRegion`.

## 5. Verify (the acceptance checks)

```powershell
python scripts\smoke.py --api-url <ApiUrl> --expect-commit <the sha that was deployed> `
  --origin https://<branch>.<appid>.amplifyapp.com --frontend-url https://<branch>.<appid>.amplifyapp.com
```
Exit code `0` means every step ran and passed; `1` means a failure; `4` means a partial run (something was skipped, for example `--skip-model`), which is **not** full verification.
The smoke test checks, against the real deployment: `/health` commit and live mode; CORS (the hosted origin is accepted, others are not, no credentials header);
our error envelope through API Gateway; real extraction; confirm; optimize against the canonical figures (1440 / 540 / 900); tamper-refused explain; text proposal
that changes nothing; explicit apply; that the base scenario is untouched; and, with `--frontend-url`, that the shipped JavaScript contains no credentials and the SPA rewrite works.

Then do the same flow **by hand in the hosted browser** (extract → confirm → optimize → certificate → proposal → apply), with the browser dev tools open: the network
requests must go only to `ApiUrl`, carry no `Authorization` or AWS headers, and succeed without CORS errors.

### Sanitized evidence

```powershell
aws logs tail <LogGroupName> --region <region> --since 15m --format short
aws logs filter-log-events --log-group-name <LogGroupName> --region <region> --filter-pattern '"<a request id from the smoke output>"'
```
Log lines hold method, route template, status, duration, request id and error code only. They never contain request bodies, descriptions, scenarios, model output or credentials
(this is tested). Copy only those lines into the compliance record. The smoke script prints the matching `X-Request-Id` values.

### 5b. Security limits and the budget alert (B8)

What the API refuses, and the status a client sees:

| Request | Answer |
|---|---|
| `POST`/`PUT` carrying an `Origin` header that is not exactly one of `ALLOWED_ORIGIN` | `403 ORIGIN_NOT_ALLOWED`, nothing is processed |
| Body not declared `application/json` (for example `text/plain` or a form post) | `415 UNSUPPORTED_MEDIA_TYPE` |
| Body over 32 KiB | `413 PAYLOAD_TOO_LARGE`, before parsing |
| Too long a text, too many items, minutes over 1,000,000, unknown fields | `422 VALIDATION_ERROR` with field paths only |
| Control or direction-override characters in a string | `422 VALIDATION_ERROR` with the field path |
| More than `CLIENT_RATE_PER_MINUTE` (default 60) requests per minute from one address, burst `CLIENT_BURST` (default 30) | `429 THROTTLED` with `Retry-After` |

Limits, in the order they apply, with where each one lives:

1. API Gateway throttle (`ThrottleRatePerSecond`, `ThrottleBurst`): global, enforced by AWS.
2. Lambda reserved concurrency (`ReservedConcurrency`): global, enforced by AWS.
3. `CLIENT_RATE_PER_MINUTE` / `CLIENT_BURST`: per client address but **per container**. Each warm Lambda container keeps its own counters, so this limit stops one client monopolising a container; it is not a global quota and it is not authentication.
4. `MAX_IN_FLIGHT`: most model or solver calls one container runs at once.

An `Origin` header is easy to forge outside a browser, so the origin check protects people using browsers; it is not authentication.

**Budget alert.** Add `-MonthlyBudgetUsd 20 -BudgetAlertEmail you@example.com` to the deploy command to create an AWS Budget that e-mails you at 50% and 80% of actual and 100% of forecast spend.
The budget covers the **whole account**, because a budget cannot be limited to this stack without cost-allocation tags. A budget is an alert, **not a spending cap**: it does not stop or throttle anything, and billing data can lag by hours.
Your role needs permission to create budgets; if it has none, create the same alert by hand under Billing and Cost Management, Budgets (section 2b). The hard limits remain reserved concurrency, the API throttles, the 32 KiB cap and the per-call token cap.

Logs: every log line passes through a redaction step (AWS key shapes, labelled secrets, bearer tokens, private keys, e-mail addresses, long token-like strings) and tracebacks are cut to the exception type. This is pattern based and best effort; the code also never writes request bodies or model text to logs.

### 5a. Optional: stored profiles (B7)

Off by default. Add `-EnableProfiles` (and optionally `-ProfileTtlSeconds <300..604800>`, default 86400) to the deploy command to create one
on-demand DynamoDB table and let the function call only `PutItem` and `GetItem` on it. `EnableProfiles` is the matching template parameter.

- **Fictional data only.** Never store real people or real businesses. The server runs a best-effort check on business names; it is not a guarantee.
- **The id is not authentication.** The client invents a random id (at least 32 URL-safe characters) and the server refuses weak ones.
  Anyone who learns an id can read that profile. There is no listing, overwrite or delete route; a profile simply expires.
- **Expiry.** Each item has an `expiresAt` epoch-seconds TTL. DynamoDB may take up to 48 hours to physically remove an expired item; the API
  treats an expired item as 404 immediately.
- **Redeploying without `-EnableProfiles` deletes the table** and everything in it (the table's deletion policy is Delete).

After deploying with `-EnableProfiles`, verify (these are NOT RUN until you run them):

```powershell
python scripts\smoke.py --api-url <ApiUrl> --expect-commit <sha> --origin <origin> --profiles
aws dynamodb describe-table --table-name <ProfilesTableName output> --region <region> --query "Table.[TableStatus,BillingModeSummary.BillingMode]"
aws dynamodb describe-time-to-live --table-name <ProfilesTableName output> --region <region>
```

`describe-time-to-live` should report `TimeToLiveStatus` `ENABLED` with attribute `expiresAt`.

## 6. After the demo: stop the spending

```powershell
.\scripts\deploy.ps1 -Teardown -Region <region>      # asks for the account id; runs: sam delete --stack-name trademind-api
```
Deleting the stack deletes the log group and its evidence, so save it first. Also delete the Amplify app and the SAM-managed bucket (`aws-sam-cli-managed-default` stack) if you no longer need them.

## 7. Troubleshooting

| Symptom | Likely cause and what to check |
|---|---|
| `/health` works, `/extract` is `502 MODEL_ACCESS_DENIED` | Model access not enabled in this region, or `BedrockResourceArns` is missing the foundation-model ARN of a region an inference profile routes to |
| `502 MODEL_NOT_FOUND` / `MODEL_REQUEST_REJECTED` | Wrong model id for this region, or the model needs an inference profile |
| `503 NOT_CONFIGURED` | `BEDROCK_MODEL_ID` empty (the template requires it, so check the deployed environment) |
| Browser shows a CORS error | `AllowedOrigin` is not exactly the Amplify origin (https, no trailing slash, no path). Redeploy with the right value |
| `/optimize` is `503 NOT_IMPLEMENTED` | `optimizer/api.py` was missing when the package was staged |
| Stack fails on reserved concurrency | Account concurrency limit too low; see section 4 |
| Deep links on the hosted site are 404 | The Amplify Rewrite (200) rule is missing or placed after another rule (rules apply top to bottom) |
| `429 THROTTLED` under light use | API throttles (`ThrottleRatePerSecond`, `ThrottleBurst`) or Bedrock quotas; raise parameters deliberately |

## 8. Cost notes

Estimate from sourced regional rates before relying on any number: Lambda requests and GB-seconds, HTTP API requests, CloudWatch Logs ingestion and storage, Bedrock tokens
(per model), the SAM bucket, and Amplify build minutes and hosting. This project does **not** include measured costs. The guards against runaway spend are:
reserved concurrency, API throttles, a 32 KiB request cap, a per-call output-token cap, at most two model calls per request, and teardown.
