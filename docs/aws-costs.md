# TradeMind: AWS cost worksheet and operations runbook (B10)

Prices retrieved **2026-11-07** from AWS's own **Price List** files (the machine-readable files AWS publishes at
`https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<offer>/current/us-east-1/index.json`), region **us-east-1** (N. Virginia), on-demand, list price in USD.
Lambda and API Gateway rates were also read on their official pricing pages the same day (`aws.amazon.com/lambda/pricing`, `aws.amazon.com/api-gateway/pricing`) and agree with the files.
Third-party price sites turned up in a web search and were **not** used.

**This is an estimate from stated assumptions. It is not a bill.** It excludes free tiers, taxes, credits, support plans and the items listed under "Not included".
Recompute with `python -m backend.telemetry --scenario demo|busy --model claude-haiku-4-5-us-profile|nova-2-lite`; a test fails if the totals below drift from the model.

## 1. Rates used (US East, N. Virginia)

| Service | Item | Rate | Price List offer (published) |
|---|---|---|---|
| Bedrock, Claude Haiku 4.5, **`us.` inference profile** (what the stack is configured with) | input tokens | $1.10 per 1M | AmazonBedrockFoundationModels (2026-10-12) |
| | output tokens | $5.50 per 1M | |
| Bedrock, Claude Haiku 4.5, **global** profile (for comparison) | input / output | $1.00 / $5.00 per 1M | same |
| Bedrock, Amazon Nova 2 Lite, on demand | input tokens | $0.33 per 1M ($0.00033 per 1K) | AmazonBedrock (2026-11-06) |
| | output tokens | $2.75 per 1M ($0.00275 per 1K) | |
| Lambda (x86, first tier, up to 6 billion GB-seconds a month) | duration | $0.0000166667 per GB-second | AWSLambda (2026-10-30) |
| | requests | $0.20 per 1M | |
| API Gateway HTTP API (first 300M requests) | requests | $1.00 per 1M | AmazonApiGateway (2026-10-13) |
| CloudWatch Logs (Standard class) | ingestion | $0.50 per GB | AmazonCloudWatch (2026-11-04) |
| | storage | $0.03 per GB-month | |
| Amplify Hosting | build minutes | $0.01 per minute | AWSAmplify (2026-09-11) |
| | build artifact storage | $0.023 per GB-month | |
| | data served | $0.15 per GB | |
| DynamoDB on demand (only if profiles are enabled, B7) | writes / reads | $0.625 / $0.125 per 1M request units | AmazonDynamoDB (2026-11-05) |
| | storage | $0.25 per GB-month after the first 25 GB | |

The `us.` profile costs 10% more than the `global.` profile (the Price List lists a "geo" rate and a "global" rate). Choosing the global profile is a cost lever; it changes where the model may run, so it is a decision for the owner.
Amazon S3 is **not enabled** (B9 was skipped), so it has no line.

## 2. Usage assumptions

One "session" is a person going through the whole demo once. Each session makes these requests:

| Request | Model call | Billed Lambda time | Basis |
|---|---|---|---|
| `GET /health` | no | 3 ms | **measured** (REPORT lines, warm) |
| `POST /extract` | yes: 694 in, 386 out tokens | 1,500 ms | tokens and time **measured** on one live call |
| `POST /confirm` | no | 3 ms | **measured** |
| `POST /optimize` | no | 5 ms | **assumed** (optimizer is not deployed yet, nothing to measure) |
| `POST /explain` | yes: 600 in, 200 out | 1,500 ms | **assumed** |
| `POST /simulate` (text proposal) | yes: 500 in, 150 out | 1,500 ms | **assumed** |
| `POST /simulate` (apply) | no | 5 ms | **assumed** |

Other assumptions, all **assumed** unless stated: Lambda memory 512 MB (the deployed value); cold starts 30 a month (demo) or 600 (busy) at **3,300 ms** billed each (**measured**: two cold starts billed 3,338 and 3,212 ms);
1 KiB of log per request; 14-day log retention (the deployed value); 10 frontend builds a month at 3 minutes; 5 MB of site; 1 page load per session at 0.1 MB; profile storage **off**.
The Nova rows reuse the Claude token counts. Tokenisers differ between models, so Nova's real counts are **UNKNOWN** until measured.

## 3. Estimated monthly cost (list price, free tiers excluded)

Scenario A, **demo**: 100 sessions in a month (700 API requests, 300 model calls, 179,400 input and 73,600 output tokens).
Scenario B, **busy**: 10,000 sessions in a month (70,000 API requests, 30,000 model calls, 17.94M input and 7.36M output tokens).

| Line | A, Haiku 4.5 `us.` | B, Haiku 4.5 `us.` | A, Nova 2 Lite | B, Nova 2 Lite |
|---|---:|---:|---:|---:|
| Bedrock input tokens | $0.20 | $19.73 | $0.06 | $5.92 |
| Bedrock output tokens | $0.40 | $40.48 | $0.20 | $20.24 |
| Lambda duration | $0.00 | $0.39 | $0.00 | $0.39 |
| Lambda requests | $0.00 | $0.01 | $0.00 | $0.01 |
| API Gateway HTTP API requests | $0.00 | $0.07 | $0.00 | $0.07 |
| CloudWatch Logs ingestion and storage | $0.00 | $0.03 | $0.00 | $0.03 |
| Amplify builds, storage, data served | $0.30 | $0.45 | $0.30 | $0.45 |
| **Total** | **$0.91** | **$61.17** | **$0.57** | **$27.12** |

Lines are rounded to cents separately, so a column can differ from its total by a cent. Under the free tiers (Lambda 1M requests and 400,000 GB-seconds a month; API Gateway 1M calls for new accounts for 12 months) the Lambda and API lines would be zero.

What the numbers say: **the language model is about 98% of the cost at the busy volume**; Lambda, API Gateway, logs and hosting together are under a dollar. The cheapest lever is the model choice and the token counts, not the AWS infrastructure.
Reserved concurrency (5), the API throttle and the per-call token cap (1,500) bound how large a month can get, but they are not a budget. Set a Budgets alert (docs/deployment.md section 2b and 5b); **an alert is not a spending cap**.

## 4. Measured, with the evidence

One live call on the deployed stack (2026-11-07, Claude Haiku 4.5 through the `us.` profile, the canonical business descriptions through `POST /extract`):

| What | Our application log | Independent source | Agree? |
|---|---|---|---|
| Input tokens | 694 (`bedrock_call`, `inputTokens`) | CloudWatch `AWS/Bedrock` `InputTokenCount` sum for that model: 694 | yes |
| Output tokens | 386 | `OutputTokenCount` sum: 386 | yes |
| Model latency | 1,452 ms (`latencyMs`) | `InvocationLatency` average: 1,366 ms | close (ours includes the client and network round trip, Bedrock's does not) |
| Lambda | `REPORT`: Duration 1,457 ms, Billed 1,457 ms, Memory 512 MB, Max used 97 MB | none (this is AWS's own line) | n/a |

Warm requests that make no model call billed 2 to 3 ms each (about 30 sampled `REPORT` lines). Max memory used was 94 to 97 MB of 512 MB, so 256 MB would probably be enough; that is a possible saving, but the saving is under a cent at demo volume, so it is **not** worth the risk of changing it.
This is **one** model call. It is a measurement, not a distribution: p95 and p99 latency are **UNKNOWN** until there is more traffic (the Logs Insights query below produces them).

Metrics that could not be measured and are therefore **UNKNOWN**: token counts for `/explain` and the text proposal (the optimizer is not deployed, and Anthropic models stopped working on this account after the first call, see the runbook); anything for Nova inside the application (a direct probe of Nova 2 Lite returned usage but was not an application call); real log bytes per request.

## 5. Not included (each would add to a real bill)

Free tiers and credits; sales tax or VAT; data transfer out to the internet from Lambda and API Gateway (the responses are a few KB); AWS Budgets itself (the first two budgets are free, later ones cost money, check the Budgets pricing page);
Bedrock prompt caching, batch and provisioned throughput (not used); CloudWatch custom metrics, alarms, dashboards and Logs Insights query charges ($0.005 per GB scanned, check the CloudWatch pricing page); X-Ray (not enabled); the SAM deployment bucket in S3 (a few KB); a custom domain; AWS Marketplace charges (Bedrock third-party model subscriptions are billed through Marketplace for some models, see the runbook);
the optional DynamoDB profile table (modelled in code, off in these scenarios); the optional S3 reports (not built).

## 6. Uncertainty

1. **Token counts per request are the largest unknown.** Only `/extract` is measured, from one call. Longer descriptions raise input tokens roughly in proportion (the cap is 8,000 characters each, three businesses).
2. A busier month could use the model's retries: a failed first answer causes a second call (a repair), which doubles that request's tokens. The log's `modelCalls` shows how often.
3. Prices change. The date above is the date the numbers were read; the test only checks that the worksheet and the model agree, not that AWS still charges these rates. Recheck before relying on it.
4. Billing data lags by hours, and Cost Explorer needs to be enabled. Nothing here was compared with an actual AWS bill.
5. Regional vs global inference profile rates differ by 10%; the stack uses the regional one.

## 7. Telemetry: what is logged

One JSON line per event, in the function's CloudWatch log group (14-day retention). Only these events and fields can be written (`backend/telemetry.py: EVENTS, FIELDS`); anything else is dropped and counted in `telemetryDropped`.

| Event | Fields |
|---|---|
| `request` (one per request) | `requestId`, `method`, `route` (template, never the URL), `status`, `durationMs`, `errorCode`, `mode`, `modelCalls`, `modelErrors`, `modelMs`, `inputTokens`, `outputTokens` |
| `bedrock_call` (one per model call) | `outcome`, `awsRequestId`, `modelId`, `region`, `latencyMs`, `stopReason`, `inputTokens`, `outputTokens`, `errorCode` or `errorType`, `status` |
| `client_rate_limited` | `requestId`, `client` (a 10-character hash, never the address) |
| `unhandled`, `mode_mismatch` | `requestId`, `errorType`; `appMode`, `adapterMode` |

Never logged: business descriptions, names, scenarios, model prompts or replies, request or response bodies, headers, credentials, signed URLs, client addresses. A string value must be a short token (letters, digits and `. _ : / { } ( ) -`), so a sentence or a URL is replaced by `[invalid]` even in an allowed field.
Token fields are `null` when the SDK did not return usage ("unknown"); `0` means no model call was made or the call failed. `modelCalls` counts every call including a repair retry.
Lambda also writes its own `REPORT` line per invocation (duration, billed duration, memory size, max memory, init duration); it is not ours and is the source for Lambda cost.

## 8. Runbook

### Where to look

CloudWatch, Log groups, the group named in the stack output `LogGroupName`, Logs Insights. Always start from a `requestId` (the browser shows it, it is in `X-Request-Id` and in the error body).

```
fields @timestamp, requestId, route, status, durationMs, errorCode, modelCalls, inputTokens, outputTokens
| filter event = "request" | sort @timestamp desc | limit 50
```

Latency by route:
```
filter event = "request" | stats count(*), pct(durationMs, 50), pct(durationMs, 95), pct(durationMs, 99) by route
```
Errors by cause:
```
filter event = "request" and status >= 400 | stats count(*) by route, status, errorCode
```
Tokens (the input to the cost model), per route and per day:
```
filter event = "request" and modelCalls > 0 | stats sum(modelCalls), sum(inputTokens), sum(outputTokens) by route, bin(1d)
```
Lambda billing: `filter @type = "REPORT" | stats sum(@billedDuration), avg(@maxMemoryUsed / 1000 / 1000), count(*)`.
Telemetry that had to drop something: `filter telemetryDropped > 0`.

### Symptoms

| Symptom | Likely cause | What to check, who acts |
|---|---|---|
| `502 MODEL_ACCESS_DENIED` on `/extract`, log `bedrock_call outcome=error errorCode=AccessDeniedException` | Model access. **Seen on this account:** the first Claude call worked, later calls failed with a message that the caller lacks the `aws-marketplace:ViewSubscriptions` and `aws-marketplace:Subscribe` actions needed to enable the model | **Human with account rights.** A role that cannot subscribe cannot enable Anthropic models. Either grant those actions, enable the model in the console as an admin, or use a model without a Marketplace subscription. Amazon Nova 2 Lite answered a direct `Converse` call with this role. Changing the model is a stack parameter (`BedrockModelId` and its ARNs) and a redeploy |
| `429 THROTTLED`, log `client_rate_limited` | One address over `CLIENT_RATE_PER_MINUTE` or `CLIENT_BURST` | Expected under a script; raise the stack parameters if a real group shares an address |
| `429 THROTTLED`, no `client_rate_limited` line | Container in-flight cap (`MaxInFlight`) or a Bedrock throttle (`bedrock_call errorCode=ThrottlingException`) | Check `modelErrors`; Bedrock quotas are in Service Quotas |
| API Gateway's own `429` (body not ours) | Account or stage throttle (`ThrottleRatePerSecond`, `ThrottleBurst`) | Raise deliberately; it is the global protection |
| `403 ORIGIN_NOT_ALLOWED` for the real site | `AllowedOrigin` is not exactly the Amplify origin | Redeploy with the right origin, no trailing slash |
| `415 UNSUPPORTED_MEDIA_TYPE` | A client sent a non-JSON `Content-Type` | Frontend: send `application/json` |
| `503 NOT_IMPLEMENTED` on `/optimize`, `/simulate`, `/explain` | The optimizer is not in the deployed package | Developer 1 must provide `optimizer/api.py`; redeploy |
| `503 ADAPTER_TIMEOUT`, `durationMs` near 25,000 | The model or solver was slow | `modelMs` shows which; the Lambda cap is 29 s |
| `500 INTERNAL_ERROR` | A bug or a contract mismatch; log `unhandled` has `errorType` | Use the `requestId`; no body is logged, reproduce with the canonical fixture |
| Cost higher than the worksheet | More sessions, more repairs (`modelCalls` per request above 1), longer descriptions | Run the token query above, put the real numbers into `Assumptions` and rerun `python -m backend.telemetry` |

### Routine

1. After each deploy: `GET /health` (the `commit` must match) and `python scripts/smoke.py` (docs/deployment.md section 5).
2. Weekly during a demo period: the Logs Insights error and token queries; compare the token totals with the worksheet's assumptions.
3. Before sharing the URL widely: confirm a Budgets alert exists (`-MonthlyBudgetUsd`), check reserved concurrency, and remember an alert only emails.
4. After the event: save the log evidence, then `deploy.ps1 -Teardown`, and delete the Amplify app and the SAM bucket stack (docs/deployment.md section 6).

### Credentials

Never paste access keys into a chat, a ticket or this repository. If a key or session token was exposed, end the session or deactivate the key in IAM, then sign in again. Temporary credentials expire, but they remain valid until they do.
