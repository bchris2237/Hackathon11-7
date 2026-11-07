# TradeMind — shared general guidelines

## Start here: what these files do

This kit is implementation instructions, not a built application. It does not prove deployment, completed tests or event submission. All teammates use this one shared guide; each runs their own numbered prompts. Developer 2 is the backend owner and initial repository maintainer. You can paste a .txt prompt directly into the coding assistant; you do not need to convert it to Markdown. A .md file is ordinary text with headings, lists and code blocks.

Put GENERAL-GUIDELINES.md at the root of the actual trademind-ai repository. Put the prompts directory alongside it, or keep it outside the repository and copy its contents into the assistant. Open the actual repository folder in your coding editor. Do not open only a downloaded prompt folder and let the assistant accidentally create a second unrelated app.

Recommended working order:

1. Developer 2 creates the GitHub repository, invites both teammates and clones it locally.
2. Developer 2 pastes PHASE-0-SETUP.txt into the assistant with this guide accessible.
3. All three review the shared models, units, API shapes and fixture. Developer 2 commits/pushes the foundation.
4. Developers 1 and 3 clone or pull that same foundation. Each developer creates their own branch.
5. Developer 1 runs A1, Developer 2 runs B1, Developer 3 runs C1 concurrently.
6. Each completes prompts 1–6 sequentially in their own track, integrating at the checkpoints below.
7. Finish the core live demonstration before any enhancement. Prompts 7–8 are High and 9–10 Optional.
8. Run the final audit, rehearse, and verify actual submission.

Do not give one assistant all 30 prompts and ask it to execute everything in one pass. A short instruction such as "do B1" is sufficient only when the assistant can read the correct file; pasting the full prompt avoids ambiguity. Each .txt implementation prompt repeats the frozen contract deliberately, so it remains usable in a fresh chat.

## What to say to the coding assistant

Before each numbered prompt, use this short instruction if needed:

```text
We are working in this existing TradeMind repository. Read
GENERAL-GUIDELINES.md and repository instructions first. Implement
only the prompt pasted below, within my ownership. Inspect existing
code before editing. Run the relevant checks and report actual results.
Do not claim unrun tests or undeployed AWS features are working.
Stop after this prompt and give me a reviewable summary.
```

If you start a new chat, tell it your developer number, current prompt, branch, last completed prompt and any unresolved integration issue. Ask it to inspect the repository rather than reconstructing state from memory. Never paste AWS credentials, access tokens or passwords into chat.

If it returns only a plan, say: "Implement the authorized local changes now, run the available checks, and explain the results. Identify any genuine external access blocker precisely." If it asks a routine design question already answered here, ask it to use the frozen contract. If requirements actually conflict, resolve the shared contract with all teammates rather than letting different assistants choose different answers.

## Exact Phase 0 completion criteria

Phase 0 creates shared infrastructure for development; it is not the cloud deployment milestone. It must provide working startup commands, generated/shared types, schemas and canonical fixtures. A frontend shell and local health endpoint must work. It must not fabricate successful extraction or solver output in live mode.

Developer 2 owns initial creation of shared files and the minimal startup scaffold, including initial frontend files. After the foundation is reviewed and committed, frontend ownership transfers to Developer 3. Subsequent changes to frontend files require their coordination. Developer 1 owns optimizer implementation after the same handoff. This prevents three assistants from simultaneously rewriting the same package configuration.

Before handing off, record in docs/phase-0-checklist.md: repository commit, selected tool versions, exact install commands, exact frontend/backend startup commands, health URL, schema parity test output, fixture validation output, and outstanding limitations. Teammates must confirm they can reproduce startup from a fresh checkout. AWS credentials are not a Phase 0 requirement. Runtime endpoints not yet implemented should report an explicit unimplemented/unavailable error, not plausible invented results.

The separate setup prompt is not one of the 30 feature prompts. It may establish placeholders for imports, but those placeholders must be reported incomplete, must fail honestly, and must not be wired as successful production behavior.

## Daily working loop and Git instructions

Use one shared GitHub repository and separate local clones. The repository folder is the application workspace. A branch is a version of that workspace, not a second app. Developer 1 uses feature/optimizer, Developer 2 feature/backend, Developer 3 feature/frontend. The initial scaffold is on main before these branches start.

At the beginning of work, inspect git status. Do not switch branches or merge with unrelated uncommitted changes. Commit intentional work or deliberately stash it first. Fetch remote updates; merge reviewed main changes into your branch when the maintainer announces a checkpoint. Do not blindly pull a different teammate's branch.

```bash
git status
git fetch origin
git switch feature/backend
git merge origin/main
```

The example is for Developer 2; others substitute their own branch name. If your branch does not exist yet, create it once with `git switch -c feature/backend` from the reviewed foundation. Do not repeatedly create branches for the same track.

After a prompt: inspect git diff, run checks, stage exact owned files, inspect the staged diff, and commit. Push your branch. Do not use an unconditional `git add .` when generated credentials, local configs or another teammate's changes might be present. Never commit .env, AWS credentials, node_modules, Python caches or secrets in logs.

The maintainer merges reviewed contributions into an integration branch, runs joint checks and promotes to main. If a conflict touches another owner's code, have that owner help resolve it. Never resolve a conflict by deleting an entire implementation or accepting one whole side without review. Never force-push shared main. Tag a known working release before the final demo so you can identify and recover it without guessing which commit worked.

## Definitions of done and how to review generated code

For each prompt, check five things: the feature follows the contract; success and failure paths work; relevant tests actually ran; explanation names the important files/functions; and capability/deployment status is truthful. "Code generated" is not equivalent to "feature verified." "Test passed with a mock" is not equivalent to "AWS integration verified."

Ask the assistant to show the request entering the system and follow it to the output. For backend, identify validation, model invocation, deterministic calls and error mapping. For optimizer, identify the search space, feasibility checks, scoring and independent verification. For UI, identify source state, pending request handling, derived display state and stale-result invalidation.

Each teammate must explain their own code and understand the other two subsystems. After each integration checkpoint, do a short walkthrough: Developer 1 explains the mathematical example; Developer 2 traces the hosted request and IAM role; Developer 3 traces user input, confirmation and rendering. Each teammate then repeats the complete flow in their own words. Human confirmation stays pending until it actually happens.

A good completion response lists changed files, commands and observed results, incomplete items, design decisions, three likely judge questions with grounded answers and the next integration step. If the assistant says "should work," treat that as an unverified claim and request the relevant check. If checks cannot execute, record why instead of repeatedly asking for fictional confidence.

## Shared contract decisions that must not drift

Use integer minutes in all internal/wire calculations. The UI may display hours, but it must validate conversion and never round before capacity or savings checks. Service names are the frozen enumeration. IDs identify entities; display names are not identity and need not be unique. Reject unknown fields and invalid references. Missing estimates mean unsupported or unknown, never instantaneous work.

For the maximum candidate bound, each of six deliverables can have at most five providers: 5^6 = 15625. That bound makes exact enumeration practical for the selected scope but does not establish unbounded scalability. Do not advertise the MVP as solving arbitrary business networks.

Baseline means the owner performs its own required work. Actual effort means production performed by the provider plus that business's coordination charge when involved in an exchange. Charge coordination once per participating business. Every business must remain no worse off individually. No-exchange is a valid outcome; it is not a server failure. Infeasible is a different outcome with no valid allocation. Missing baseline information is an input validation failure, not proof that a valid business arrangement cannot exist.

Natural-language simulation is two stages: propose then explicitly confirm/apply. The model cannot directly mutate the scenario. Do not accept arbitrary executable code, arbitrary JSON paths or invented IDs from model output. The deterministic engine recalculates assignments and certificates after a valid approved change. Preserve the original scenario for comparison.

The certificate proves recomputed arithmetic and constraints under supplied assumptions. It is not a signed legal document, identity credential, guaranteed business profit, payment agreement or guarantee of equal service quality. Hashes detect input differences but are not a substitute for signatures or trusted identity. Every presentation must preserve those distinctions.

## File ownership and shared changes

- Developer 1: `optimizer/**`, `tests/optimizer/**`.
- Developer 2: `backend/**`, `infra/**`, `scripts/**`, `tests/backend/**` and maintenance of shared contracts/scaffold after team agreement.
- Developer 3: `frontend/**` and `tests/e2e/**`.

Each developer owns their prompt-specific docs/compliance record. Shared README sections can be drafted separately and assembled by the maintainer to avoid simultaneous edits.

If you need another owner's change, provide a small request: affected file/interface, reason, desired behavior, compatibility impact and acceptance example. Do not let an assistant silently edit their files. Version shared schema changes and regenerate affected types/fixtures together. Do not use temporary ad hoc frontend aliases to hide a broken API contract.

The backend must remain independently testable before the optimizer merges by injecting a fixture adapter in tests. The frontend can use explicit fixtures before AWS access exists. That independence does not justify wiring fixtures into the live path. Production startup should reject missing real dependencies. Fixture mode needs a persistent visible label and must not be a silent fallback.

## AWS setup and evidence policy

Developer 2 manages cloud configuration. Use the team's authorized AWS account and intended region, with credentials supplied through the standard AWS credential chain locally and execution roles in Lambda. Never include AWS credentials in Vite variables, browser bundles or git commits. Only the public API URL and display mode belong in frontend environment variables.

Use the configured Bedrock model available to the account and region. Do not assume a model identifier, inference profile, permission or structured-output capability exists. At implementation time, consult the current official service documentation for chosen APIs and prices. Record exact model/region configuration and explain the required IAM resources, including inference-profile permissions if applicable. Model access and cloud account restrictions may require human action.

Deploy infrastructure with the repository's documented SAM commands after inspecting the target configuration. Confirm stack outputs, Lambda/API connectivity and Amplify configuration. Verify success with an actual request, not just a successful build. CORS must allow the actual frontend origin; secrets never belong in CORS configuration or URLs. Budget alerts are warnings, not guaranteed hard spending caps. Use bounded model output, retry limits, throttling and concurrency controls in the critical implementation.

Cloud proof should include deployed URL, commit, timestamp, region/model identifier and sanitized request/log references tied to actual output. Avoid recording full private descriptions, credentials or signed download URLs. Check optional DynamoDB/S3 independently if claimed. The README architecture should show only genuinely implemented services and distinguish planned enhancements.

## Security and failure-handling minimums

Reject oversized bodies, invalid types, unknown IDs, unsupported patches and missing required values before expensive model calls where possible. Treat model output and user descriptions as untrusted data. Log request IDs and safe metadata instead of secrets. Return consistent error envelopes without internal stack traces. Keep frontend form data when recoverable errors occur, invalidate stale outputs after edits and prevent late responses from overwriting newer state.

Public demo mode has no general-purpose user authentication unless separately implemented. Use fictional data only. Unguessable profile IDs are not authentication. Do not present optional persistence as private multi-tenant storage. Public cloud deployments incur real costs even when used only for judging; document how to disable/delete resources after the event and preserve evidence before cleanup.

## Testing strategy and troubleshooting

Start with the smallest relevant local test, fix the defect, then rerun affected checks. At integration checkpoints run the complete critical suite and hosted workflow. Do not repeatedly run unrelated expensive suites without a change or unresolved concern. Maintain separate local fixture tests and live AWS smoke evidence.

- If install fails, inspect the actual error, runtime versions and lockfiles; avoid randomly upgrading the whole stack.
- If schema validation fails, compare the frozen schema with the actual JSON; do not relax validation merely to make a test pass.
- If /health works but extraction fails, inspect model access, region, request shape and backend logs.
- If the API works directly but the browser fails, inspect the browser network request, public API URL, CORS and frontend build configuration.
- If output totals differ, verify minutes, baseline estimates, assignments and coordination counting before changing the expected fixture.

If a dependency in another track is unfinished, keep implementing against its frozen interface with a test adapter and record the pending live merge. If a cloud permission is unavailable, identify the exact permission or account action and continue independent local work. Never mark a blocked feature deployed. If a prompt starts changing unrelated architecture, stop that drift and bring it back to the shared contract.

## Managing time and assistant usage

Run one prompt at a time. Keep explanations concise but sufficient for understanding. Provide concrete error output rather than asking the assistant to restart the app from scratch. Reuse the repository as the source of truth and start a new chat with a short factual handoff if the old chat becomes confused. Do not send secrets in error logs.

This kit does not estimate how many Kiro credits each prompt consumes or guarantee that a particular credit allowance completes the project. Watch the actual account usage. Finish the eighteen Critical prompts and early integration before High/Optional work. A single live complete flow is more valuable than ten unmerged enhancements.

## Team compliance and final release policy

Every prompt writes its own compliance record: design explanation, important functions, tests actually run, limitations, judge questions and human understanding status. Classify each capability IMPLEMENTED, SIMULATED or INCOMPLETE. Track deployment separately as VERIFIED, UNVERIFIED or N/A. A local working feature can be IMPLEMENTED with deployment UNVERIFIED. Mixed capabilities should be split into separate rows.

Before submission, verify live AWS usage, complete deployed demonstration, arithmetic, business-model assumptions and operating costs, actual repository submission, all three Devpost project members and all three human understanding reviews. Recheck official event rules and deadlines at submission time. Prepared files are not proof of a completed submission. A drafted invitation is not proof of membership. External checks unavailable to the assistant need dated human confirmation, explicitly attributed.

Do not claim legal certification or organizer approval. A GO/NO-GO audit is your team's internal assessment. Keep README, demo video, slides and Devpost description consistent with actual evidence. If live services fail during judging, explicitly identify a recorded demo or fixture fallback as such.

---

## Reference architecture, exact contracts and canonical fixture

The following reference is carried forward from the supplied playbook. It contains the definitive data shapes and mathematical expectations used by all thirty copied prompts. If this guide's operational advice and a copied prompt ever conflict, pause the conflicting change and resolve it with the maintainer; do not silently change the frozen wire contract.

# TradeMind AI — executable three-developer playbook

This is the generated implementation playbook, not another prompt generator. The separate master specification was not supplied; assumptions below resolve the requirements present in the attachment. No application has been built or deployed by producing this document.

## PART A — Final technical architecture and Phase 0

Use React + Vite + TypeScript, Tailwind and @xyflow/react; Python 3.12, Pydantic 2, boto3, pytest and Hypothesis; SAM-managed Lambda/HTTP API and Amplify Hosting. DynamoDB and S3 are optional. Bedrock extracts input and interprets proposed changes; deterministic Python decides assignments and verifies arithmetic. This avoids native solver packaging for a maximum 15625 assignments. The objective is hours of effort under equal-quality deliverables, not market pricing, transferable utility or guaranteed profit.

Phase 0 is shared setup, not an additional feature prompt. Developer 2 creates the following files while teammates review contracts. Select compatible stable package releases using current official package documentation, pin exact resolved versions in package-lock.json and Python requirements lock, record them in docs/versions.md, and freeze before track work. This document intentionally does not assert unverified current package versions. Frontend package scripts: dev, build, typecheck, test (Vitest); root pyproject supports editable installation and dev tests. Install React Testing Library and Playwright for UI verification. Add .gitignore for .env, credentials, node_modules, caches and build outputs; .env.example contains names only.

```text
contracts/scenario.schema.json, result.schema.json, api.openapi.yaml
contracts/models.ts, models.py
fixtures/canonical.json, coordination.json, disruption.json, extraction.json
optimizer/                    # Developer 1
backend/                      # Developer 2
infra/template.yaml           # Developer 2
scripts/                      # Developer 2
frontend/src/                 # Developer 3
tests/optimizer/, tests/backend/, tests/e2e/
docs/architecture.md, versions.md, deployment.md, business-model.md
docs/compliance/
pyproject.toml, requirements.lock, README.md
```

Create JSON Schema as canonical source and generate TS/Pydantic equivalents, with fixture parity checks. Freeze endpoint contracts before implementation. Phase 0 scaffold renders a page and serves health; fixture adapters explicitly identify simulated behavior and never live in production dispatch. Store canonical fixture, its 60-minute coordination variant, disruption patch and extraction draft as JSON. Local startup: `python -m backend.local`; `npm --prefix frontend run dev`. AWS environment: AWS_REGION, BEDROCK_MODEL_ID, ALLOWED_ORIGIN, APP_MODE, COMMIT_SHA, optional PROFILE_TABLE and REPORT_BUCKET; local profile may use AWS_PROFILE, Lambda uses execution role. Frontend only VITE_API_BASE_URL and VITE_DATA_MODE, never AWS credentials.

### Frozen schema, mathematics and API contract

TradeMind v1 frozen contract. Work inside the existing monorepo, never create a separate app. Python 3.12/Pydantic 2 backend and pure optimizer; React/Vite/TypeScript/Tailwind/React Flow frontend. Use integer minutes internally, never floating-point hours for feasibility. Currency/profit is outside the model.

Business={id,name,capacityMinutes,coordinationMinutes,estimates:[{service,minutes}]}; IDs unique nonempty ASCII slugs; service is coding|design|writing; minutes positive integers; capacity/coordination nonnegative integers. Unique service estimates per business. Deliverable={id,ownerId,service}; one indivisible unit per deliverable. Scenario={schemaVersion:'1',businesses:Business[],deliverables:Deliverable[],confirmed:true}. MVP exactly 3 businesses, 1..6 deliverables; optional extension at most 5 businesses. Missing estimates mean unsupported service, never zero effort. Each owner's estimate for its own requirement is required to define baseline; absent baseline yields 422 MISSING_BASELINE. All unknown fields rejected; bound request size to 32 KiB and numeric minutes to 1000000. Distinguish malformed input from valid but infeasible scenarios.

Assignment={deliverableId,providerId,minutes}. For business i: baselineMinutes=sum own estimates for own deliverables; productionMinutes=sum work assigned to i. Charge its coordinationMinutes once if i provides to or receives from a different business. effortMinutes=production+coordination; netSavingsMinutes=baseline-effort. Enforce effort<=capacity and netSavings>=0 for every business. Mutually beneficial collaboration additionally requires at least one cross-business assignment and strictly positive total net savings. Self-assignment is allowed. Enumerate feasible providers per deliverable; minimize total effort, then maximum individual effort/baseline ratio (zero baseline handled explicitly), then lexicographically sorted provider IDs by deliverable ID. Use integer cross multiplication for ratios. No claims about real profitability or comparative advantage beyond the supported effort model.

Result={schemaVersion:'1',status:'collaboration'|'no_exchange'|'infeasible',assignments:Assignment[],businessMetrics:[{businessId,baselineMinutes,productionMinutes,coordinationMinutes,effortMinutes,netSavingsMinutes}],totals:{baselineMinutes,productionMinutes,coordinationMinutes,effortMinutes,grossSavingsMinutes,netSavingsMinutes},reasons:[{code,message}],certificate:Certificate|null}. Totals/metrics nullable only when infeasible; infeasible assignments empty. no_exchange means feasible allocation exists but no strictly beneficial exchange; do not call it infeasible. Certificate={schemaVersion:'1',scenarioDigest,allocationDigest,verification:'valid',assumptions:string[],checks:[{name,passed:true}]}. Digests use SHA256 of canonical JSON with recursively sorted keys, array order retained, UTF-8 without whitespace; backend is authoritative. Verification is deterministic recomputation, not a signature or a business guarantee.

POST /extract {descriptions:[{id,name,text}]} returns {draft:{businesses:[{id,name,capacityMinutes:null|integer,coordinationMinutes:null|integer,estimates:[{service,minutes}]}],deliverables:Deliverable[]},missingFields:string[],provenance:[{path,quote}],mode:'live'|'fixture'}. Missing numbers remain null/absent; extraction never sets confirmed. POST /confirm {scenario:Scenario} validates and returns {scenario:Scenario}. POST /optimize {scenario:Scenario} returns {result:Result,mode:'live'|'fixture'}. POST /simulate {scenario:Scenario,changes:Change[],text?:string,apply:boolean} returns {proposedChanges:Change[],requiresConfirmation:boolean,scenario:Scenario,result:Result|null,mode:'live'|'fixture'}. Change is tagged {kind:'capacity'|'coordination',businessId,minutes} or {kind:'estimate',businessId,service,minutes}. Reject text plus nonempty changes. Text is proposal-only (apply must be false); explicit validated changes with apply=true recalculate without Bedrock. Preserve original scenario. POST /explain {scenario:Scenario,result:Result} returns {text,mode}; recompute result before giving verified values to Bedrock; never trust client math. GET /health returns {status:'ok',schemaVersion:'1',commit,mode}. Errors everywhere {error:{code,message,fields:string[],requestId}} with 400 malformed JSON, 422 validation, 429 throttled, 502 model failure, 503 unavailable; no fake successful fallbacks.

Optional endpoints: PUT /profiles/{id} {scenario} and GET /profiles/{id} return {id,scenario}; POST /certificates {scenario,result} returns {certificate,downloadUrl?:string,expiresAt?:string}; recompute verification before storage. Core certificate is embedded in optimize/simulate and needs no optional route.

Canonical fixture: devco needs design; designco needs writing; copyco needs coding. Each capacity=600, coordination=0. Devco estimates coding=180,design=480; Designco design=180,writing=480; Copyco writing=180,coding=480. Other services unsupported. Deliverables d-design,d-writing,d-coding belong to devco,designco,copyco respectively. Baseline=1440 minutes, optimum production=540, gross savings=900 (62.5%); each produces 180 and saves 300. With coordination=60 each, effort=720 and net savings=720. Disruption: designco design estimate=600 leaves self-production as valid no_exchange; capacity=0 for every business is infeasible. Never hardcode solver output.

Ownership: Developer 1 optimizer/** and tests/optimizer/**; Developer 2 backend/**,infra/**,scripts/**,tests/backend/** and initial root scaffold/contracts/**/fixtures/**; Developer 3 frontend/** and tests/e2e/**. Shared contract edits require a joint versioned decision. Each owns docs/compliance/<prompt-id>.md. Dependencies across tracks are contracts/fixtures, not unfinished source. Temporary adapters belong only in tests or explicit fixture mode; live startup must reject missing real adapters. Required commands: `python -m pip install -e '.[dev]'`; `python -m pytest tests/optimizer tests/backend`; `npm --prefix frontend ci`; `npm --prefix frontend run typecheck`; `npm --prefix frontend test -- --run`; `npm --prefix frontend run build`. Phase 0 establishes these scripts and lockfiles. Never claim commands succeeded without running them.

## PART B — Parallel work and ownership

Developer 1 owns mathematics; Developer 2 owns cloud/API and initially maintains shared contracts; Developer 3 owns UI. Create feature/optimizer, feature/backend and feature/frontend from the frozen scaffold. Each developer runs only their track in sequence; tracks run concurrently. Work against contracts/fixtures until joint integration. Request cross-owner changes instead of editing their files. Developer 2 merges reviewed commits at checkpoints after contracts and tests pass. Record a shared contract change with migration and synchronized fixture/tests before any consumers adopt it. Never overwrite another branch or force-push shared main. Each teammate teaches their subsystem to both others and traces the complete request path.

---

## Integration, verification, submission and pitch reference

## PART D — Dependency and integration schedule

Use elapsed time rather than assuming an unverified remaining event duration. Reserve at least the final third for integration, deployment, debugging and presentation; cut enhancements first. For a 24-hour window, budget 1 hour foundation, 11 hours critical concurrent implementation with incremental merges, 8 hours integration/deployment/testing and 4 hours rehearsal/submission. Deploy a thin slice early during critical work, not after hour 12.

| Checkpoint | Developer 1 | Developer 2 | Developer 3 | Evidence and merge gate |
|---|---|---|---|---|
| 0: contract freeze | Review mathematical units | Create scaffold/contracts | Review UI types | Schemas parse same fixture; health and shell run; maintainer tags foundation |
| 1: early vertical slice | A1–A3 | B1, test adapters, begin B2 | C1–C3 | Fixture extraction→real solver→API→screen; explicitly simulated Bedrock; merge compatible changes |
| 2: critical integration | A4–A6 | B2–B6 | C4–C6 | Actual Bedrock, deterministic simulation and certificate across live stack; mandatory smoke before enhancements |
| 3: deployed release | Verify numerical evidence | Verify deployed resources/version | Rehearse hosted user flow | Tests pass, public URL works, no hidden mock; all owners review release |
| Enhancement window only | A7–A8, then A9–A10 | B7–B8, then B9–B10 | C7–C8, then C9–C10 | Skip every enhancement if critical flow or submission incomplete |

At each merge: clean working tree, fetch origin, review incoming diff, merge reviewed track commits into integration branch, run affected tests then joint smoke. Resolve conflicts with owner rather than selecting an entire side blindly. Promote to main only after passing checks. Never defer minimum security, error handling or correctness to optional work.

## PART E — Testing and verification

| Input | Required observation |
|---|---|
| Canonical | 1440 baseline, 540 production, 900 gross/net savings; each saves 300 minutes |
| Coordination 60 each | 180 coordination total, 720 effort, 720 net savings; each saves 240 |
| Designco design=600 | Self allocation feasible; no beneficial exchange, status no_exchange |
| All capacities zero | infeasible, no fabricated totals/certificate |
| Missing owner's estimate | 422 MISSING_BASELINE |
| Unsupported provider service | Provider excluded, never duration zero |
| Attractive total but one harmed | Allocation rejected |
| Multiple equal optima | Stable documented tie-break regardless of input business order |
| High coordination | Self allocation can beat collaboration |
| Tampered assignment/certificate | Independent verification rejects |
| Bedrock denied/timeout | Visible error; no silent fixture fallback |

Run Python suites, frontend typecheck/unit/build and Playwright critical path against local fixture mode first, then a separate live smoke against deployed URL. Include independent oracle and verifier tests, not assertions copied from solver internals. Record commands, timestamp, commit, expected/observed outputs and mode. Keep live test data fictional; sanitize logs. Confirm AWS resource existence AND successful usage with correlated requests. Tests not run remain NOT RUN.

## PART F — Deployment and hackathon checklist

- Freeze tested commit; verify SAM stack, Lambda version, allowed origin, actual Bedrock model access, Amplify build and public SPA refresh.
- Verify real extraction, explicit confirmation, optimization, certificate, scenario proposal and deterministic recalculation from hosted browser. Retain evidence and rehearse failure handling.
- README: purpose, architecture, setup commands, configuration names, tests, deployment, mathematical assumptions, AI assistance, team contributions and implemented/simulated/incomplete matrix.
- Business model: proposed customer is small agencies needing complementary services; proposed buyer is agency owner or local business network. Subscription or network licensing is a hypothesis, not validated demand. Explain onboarding/trust/quality/coordination barriers. Estimate AWS costs using sourced regional rates and measured/assumed request volumes separately. Time savings are modeled labor savings, not proven cash profit.
- Verify repository URL is attached to actual Devpost submission and reviewer access works. Verify all three teammates are actual project members and have reviewed all subsystems. Do not infer submission from prepared files.
- Recheck official rules, eligibility, chosen track and deadline before submitting: https://a10-x-aws-hack-26.devpost.com/rules and https://a10-x-aws-hack-26.devpost.com/ . Sources were checked November 7, 2026. AI assistance is permitted with participant understanding; this playbook's teach-back controls implement the team's stricter requirement. Do not claim event certification.
- Prepare demo video/slides/screenshots if time allows; identify recommended extras separately from mandatory repository and team membership requirements. Final audit is mandatory even if the coordination prompt is skipped.

## PART G — Three-minute evidence-based pitch

- 0:00–0:20: Small agencies may spend time on tasks outside their strengths. TradeMind proposes feasible exchanges under explicitly supplied labor estimates; demand remains a business hypothesis.
- 0:20–0:50: Enter seeded fictional descriptions, call live Bedrock, and show missing-information safeguards and human confirmation. Give each teammate a speaking role.
- 0:50–1:25: Run deterministic optimization; explain 24→9 production hours and the provider→recipient graph. Switch to 1-hour coordination per company to show 12 total effort hours; distinguish gross from net.
- 1:25–1:50: Open certificate and show recomputation, constraints and quality/valuation assumptions. Explain that verified arithmetic does not guarantee commercial outcomes.
- 1:50–2:25: Request Designco's design time become 10 hours; inspect proposed patch, confirm it and show no beneficial exchange. Explain why self-production can remain feasible. Avoid falsely calling this disruption globally infeasible.
- 2:25–3:00: Trace AWS request path, describe intended buyer/revenue hypothesis and measured-versus-estimated costs. State current limitations and each teammate's contribution.

Judge preparation:

- Why exact enumeration? At most 15625 candidates makes exhaustive correctness practical.
- Why Bedrock? Language extraction and interpretation, not numerical optimization.
- Why no-worse-off? Total savings alone can hide a harmed participant.
- What does the certificate prove? Feasibility/accounting under input assumptions, not identity or profit.
- What scales poorly? Exponential assignment count; larger networks require a redesigned bounded solver.
- What is live? Answer using final audit evidence, never architecture aspirations.
