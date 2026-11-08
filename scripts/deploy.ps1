<#
.SYNOPSIS
  Build and (only on explicit confirmation) deploy the TradeMind backend with AWS SAM.

.DESCRIPTION
  Step by step: check prerequisites, stage the Lambda package, `sam validate --lint`, `sam build`, then `sam deploy`.
  By default it STOPS after the build. It deploys only when you pass -Deploy, and even then it shows the AWS account
  and region it is about to use and waits for you to type the account id. Nothing here assumes any resource exists,
  and no credential is ever read from, written to, or printed by this script (the AWS CLI/SAM use their normal
  credential chain).

  Windows PowerShell 5.1 or PowerShell 7. Run from anywhere; paths resolve from this file.

.PARAMETER Deploy
  Actually create/update the stack. Without it, the script validates and builds only.
.PARAMETER Region
  Region to deploy to. Bedrock is called in this same region (Lambda's AWS_REGION), so it must be a region where your
  Bedrock model access is enabled. Required with -Deploy.
.PARAMETER StackName
  CloudFormation stack name. Default: trademind-api.
.PARAMETER AllowedOrigin
  Exact https origin(s) of the hosted frontend (the Amplify URL), comma separated. Required with -Deploy.
.PARAMETER BedrockModelId
  Model id or inference profile id your account may use. Required with -Deploy. Never guessed.
.PARAMETER BedrockResourceArns
  Comma-separated exact ARNs for bedrock:InvokeModel (see docs/deployment.md). Required with -Deploy. No wildcards.
.PARAMETER ReservedConcurrency
  "5" by default. "-1" skips the ceiling (only for accounts whose limit is too low); it is reported loudly.
.PARAMETER AllowMissingOptimizer
  Permit packaging when optimizer/api.py does not exist. The deployed /optimize, /simulate and /explain will then answer
  503. For an early thin-slice deploy only; the default is to refuse, because the live workflow needs the optimizer.
.PARAMETER Profile
  Optional AWS CLI profile name.
.PARAMETER Yes
  Skip the typed account-id confirmation. For automation you trust; a human should normally not use it.
.PARAMETER Teardown
  Delete the stack (after the same confirmation). Preserve your evidence first.

.EXAMPLE
  .\scripts\deploy.ps1                       # validate + build only, nothing is created
.EXAMPLE
  .\scripts\deploy.ps1 -Deploy -Region us-west-2 -AllowedOrigin https://main.d123.amplifyapp.com `
      -BedrockModelId <id> -BedrockResourceArns "<arn1>,<arn2>"
#>
[CmdletBinding()]
param(
  [switch]$Deploy,
  [string]$Region,
  [string]$StackName = "trademind-api",
  [string]$AllowedOrigin,
  [string]$BedrockModelId,
  [string]$BedrockResourceArns,
  [string]$ReservedConcurrency = "5",
  [switch]$AllowMissingOptimizer,
  [string]$Profile,
  [switch]$Yes,
  [switch]$Teardown
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Stage = Join-Path $Root ".aws-sam\stage\function"
$Template = Join-Path $Root "infra\template.yaml"
$env:SAM_CLI_TELEMETRY = "0"

function Say([string]$text) { Write-Host $text }
function Fail([string]$text) { Write-Host "ERROR: $text" -ForegroundColor Red; exit 1 }
function Need([string]$name, [string]$hint) {
  if (-not (Get-Command $name -ErrorAction SilentlyContinue)) { Fail "'$name' was not found on PATH. $hint" }
}
function Run([string]$exe, [string[]]$arguments) {
  & $exe @arguments
  if ($LASTEXITCODE -ne 0) { Fail "'$exe $($arguments -join ' ')' failed with exit code $LASTEXITCODE." }
}
$profileArgs = @(); if ($Profile) { $profileArgs = @("--profile", $Profile) }

# ---------------------------------------------------------------------------------------- 1. prerequisites
Say "== 1. Prerequisites"
Need "sam" "Install the AWS SAM CLI: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html"
Need "git" "Install Git."
Say ("sam: " + (& sam --version))
if ($Deploy -or $Teardown) {
  Need "aws" "Install the AWS CLI v2."
  if (-not $Region) { Fail "-Region is required (the region where your Bedrock model access is enabled). It is never guessed." }
}

# ---------------------------------------------------------------------------------------- 2. stage package
if (-not $Teardown) {
  Say "== 2. Stage the Lambda package"
  $apiPy = Join-Path $Root "optimizer\api.py"
  if (-not (Test-Path $apiPy)) {
    if (-not $AllowMissingOptimizer) {
      Fail "optimizer/api.py does not exist, so /optimize, /simulate and /explain would answer 503 in the cloud. Merge Developer 1's optimizer first, or pass -AllowMissingOptimizer for a thin-slice deploy."
    }
    Write-Host "WARNING: optimizer/api.py is missing. The deployed optimize/simulate/explain routes will answer 503." -ForegroundColor Yellow
  }
  if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
  New-Item -ItemType Directory -Force $Stage | Out-Null
  foreach ($pkg in @("backend", "contracts", "optimizer")) {
    $src = Join-Path $Root $pkg
    $dst = Join-Path $Stage $pkg
    New-Item -ItemType Directory -Force $dst | Out-Null
    # Python source only: no caches, no tests, no schemas/openapi/typescript (not needed at runtime).
    Get-ChildItem $src -Recurse -File -Include *.py | Where-Object { $_.FullName -notmatch "__pycache__" } | ForEach-Object {
      $rel = $_.FullName.Substring($src.Length).TrimStart("\")
      $target = Join-Path $dst $rel
      New-Item -ItemType Directory -Force (Split-Path $target) | Out-Null
      Copy-Item $_.FullName $target
    }
  }
  Copy-Item (Join-Path $Root "infra\requirements-lambda.txt") (Join-Path $Stage "requirements.txt")
  $count = (Get-ChildItem $Stage -Recurse -File | Measure-Object).Count
  Say "staged $count files in .aws-sam\stage\function"

  # ------------------------------------------------------------------------------------ 3. validate and build
  Say "== 3. sam validate --lint"
  $lintRegion = if ($Region) { $Region } else { "us-east-1" }  # lint only; no AWS call is made
  Run "sam" @("validate", "--lint", "-t", $Template, "--region", $lintRegion)

  Say "== 4. sam build"
  Push-Location $Root
  try { Run "sam" @("build", "-t", $Template) } finally { Pop-Location }
  Say "Build finished: .aws-sam\build"
}

if (-not $Deploy -and -not $Teardown) {
  Say ""
  Say "Nothing was deployed (no -Deploy). To deploy, see docs/deployment.md."
  exit 0
}

# ---------------------------------------------------------------------------------------- 5. confirm target
Say "== 5. Confirm the AWS target"
$idJson = & aws sts get-caller-identity --output json @profileArgs 2>&1
if ($LASTEXITCODE -ne 0) { Fail "Could not read your AWS identity. Sign in first (for example 'aws sso login'). Nothing was changed." }
$account = ($idJson | ConvertFrom-Json).Account
Say "AWS account : $account"
Say "Region      : $Region"
Say "Stack       : $StackName"
if (-not $Yes) {
  $typed = Read-Host "Type the account id above to continue, anything else cancels"
  if ($typed.Trim() -ne $account) { Say "Cancelled. Nothing was changed."; exit 2 }
}

if ($Teardown) {
  Say "== Teardown: deleting stack $StackName (evidence such as logs is deleted with it; save it first)"
  Run "sam" (@("delete", "--stack-name", $StackName, "--region", $Region, "--no-prompts") + $profileArgs)
  exit 0
}

# ---------------------------------------------------------------------------------------- 6. required inputs
foreach ($pair in @(@("AllowedOrigin", $AllowedOrigin), @("BedrockModelId", $BedrockModelId), @("BedrockResourceArns", $BedrockResourceArns))) {
  if (-not $pair[1]) { Fail "-$($pair[0]) is required with -Deploy. It is never guessed." }
}
if ($BedrockResourceArns -match "\*") { Fail "BedrockResourceArns must be exact ARNs. Wildcards are not allowed." }
if ($AllowedOrigin -match "\*" -or $AllowedOrigin -notmatch "^https://") { Fail "AllowedOrigin must be exact https origin(s), no wildcard." }
if ($ReservedConcurrency -eq "-1") {
  Write-Host "WARNING: -ReservedConcurrency -1 removes the hard ceiling on parallel invocations (and so on model spend)." -ForegroundColor Yellow
}
$commit = (& git -C $Root rev-parse --short=12 HEAD).Trim()
$dirty = (& git -C $Root status --porcelain | Measure-Object).Count
if ($dirty -gt 0) { Write-Host "WARNING: the working tree has $dirty uncommitted change(s); the deployed commit id will not describe exactly what was built." -ForegroundColor Yellow }

# ---------------------------------------------------------------------------------------- 7. deploy
Say "== 6. sam deploy (shows the change set and asks before executing it)"
$overrides = @(
  "AllowedOrigin=$AllowedOrigin",
  "BedrockModelId=$BedrockModelId",
  "BedrockResourceArns=$BedrockResourceArns",
  "CommitSha=$commit",
  "ReservedConcurrency=$ReservedConcurrency"
)
$deployArgs = @(
  "deploy", "-t", (Join-Path $Root ".aws-sam\build\template.yaml"),
  "--stack-name", $StackName, "--region", $Region,
  "--resolve-s3", "--capabilities", "CAPABILITY_IAM",
  "--no-fail-on-empty-changeset", "--parameter-overrides") + $overrides + $profileArgs
if (-not $Yes) { $deployArgs += "--confirm-changeset" }
Run "sam" $deployArgs

# ---------------------------------------------------------------------------------------- 8. describe
Say "== 7. Stack outputs"
Run "aws" (@("cloudformation", "describe-stacks", "--stack-name", $StackName, "--region", $Region,
  "--query", "Stacks[0].Outputs[].[OutputKey,OutputValue]", "--output", "table") + $profileArgs)
Say ""
Say "Next: python scripts\smoke.py --api-url <ApiUrl> --expect-commit $commit --origin $AllowedOrigin"
Say "Then set VITE_API_BASE_URL (ApiUrl) and VITE_DATA_MODE=live in Amplify and redeploy the frontend. See docs/deployment.md."
