<#
.SYNOPSIS
  Build the frontend in LIVE mode and publish it to an Amplify Hosting app by manual deployment (no GitHub connection needed).

.DESCRIPTION
  The frontend is a static site (index.html, one JS file, one CSS file). This script:
    1. builds it with VITE_API_BASE_URL and VITE_DATA_MODE=live baked in (public values only),
    2. refuses to publish if the bundle contains a credential-looking string or does not contain the API address,
    3. zips it with FORWARD-SLASH entry names (Windows PowerShell's Compress-Archive writes backslashes, and Amplify then stores
       "assets\index.js" as one file at the root, so the page loads but its scripts are 404 and the site is blank),
    4. uploads the zip, starts the deployment and waits for it to finish,
    5. fetches the public page and every asset it references and requires HTTP 200 for each.
  Nothing is deployed unless every check before the upload passes. It makes AWS calls only against the app you name.

  Prerequisite: an Amplify app and a branch already exist (see docs/deployment.md), and you are signed in to AWS.

.PARAMETER AppId
  Amplify app id, for example d2pd0bxuetqhvk.
.PARAMETER ApiUrl
  The API base URL (stack output ApiUrl).
.PARAMETER Region
  AWS region of the Amplify app. Required, never guessed.
.PARAMETER Branch
  Amplify branch name. Default "main".
.PARAMETER Profile
  Optional AWS CLI profile name.

.EXAMPLE
  .\scripts\deploy_frontend.ps1 -AppId d2pd0bxuetqhvk -Region us-east-1 -ApiUrl https://abc123.execute-api.us-east-1.amazonaws.com
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$AppId,
  [Parameter(Mandatory = $true)][string]$ApiUrl,
  [Parameter(Mandatory = $true)][string]$Region,
  [string]$Branch = "main",
  [string]$Profile
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
function Fail([string]$text) { Write-Host "ERROR: $text" -ForegroundColor Red; exit 1 }
function Say([string]$text) { Write-Host $text }

if ($ApiUrl -notmatch "^https://[A-Za-z0-9.-]+(/[A-Za-z0-9._~/-]*)?$") { Fail "-ApiUrl must be an https URL (the stack output ApiUrl)." }
if ($AppId -notmatch "^[a-z0-9]{10,20}$") { Fail "-AppId does not look like an Amplify app id." }
$ApiUrl = $ApiUrl.TrimEnd("/")
$profileArgs = @(); if ($Profile) { $profileArgs = @("--profile", $Profile) }
foreach ($tool in @("npm", "aws")) { if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Fail "'$tool' was not found on PATH." } }

# ---------------------------------------------------------------------------------------------- 1. build
Say "== 1. Build the frontend (live mode, API $ApiUrl)"
$env:VITE_API_BASE_URL = $ApiUrl
$env:VITE_DATA_MODE = "live"
& npm --prefix (Join-Path $Root "frontend") run build
if ($LASTEXITCODE -ne 0) { Fail "The frontend build failed." }
$dist = Join-Path $Root "frontend\dist"
if (-not (Test-Path (Join-Path $dist "index.html"))) { Fail "frontend/dist/index.html is missing after the build." }

# ---------------------------------------------------------------------------------------------- 2. inspect
Say "== 2. Inspect the bundle"
$bundleText = ""
Get-ChildItem $dist -Recurse -File -Include *.js, *.html, *.css | ForEach-Object { $bundleText += [IO.File]::ReadAllText($_.FullName) }
$credential = '(AKIA|ASIA)[0-9A-Z]{16}|aws_secret_access_key|X-Amz-Security-Token|VITE_AWS|BEGIN (RSA |EC )?PRIVATE KEY'
if ($bundleText -match $credential) { Fail "The bundle contains a credential-looking string. Nothing was uploaded." }
if (-not $bundleText.Contains($ApiUrl)) { Fail "The bundle does not contain the API address $ApiUrl. Nothing was uploaded." }
Say "   no credential patterns; the API address is in the bundle."

# ---------------------------------------------------------------------------------------------- 3. zip (forward slashes)
Say "== 3. Zip with forward-slash entry names"
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
$zipPath = Join-Path ([IO.Path]::GetTempPath()) ("trademind-site-" + [guid]::NewGuid().ToString("N") + ".zip")
$distFull = (Resolve-Path $dist).Path
$zip = [IO.Compression.ZipFile]::Open($zipPath, [IO.Compression.ZipArchiveMode]::Create)
try {
  Get-ChildItem $distFull -Recurse -File | ForEach-Object {
    $rel = $_.FullName.Substring($distFull.Length + 1).Replace("\", "/")
    [void][IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $_.FullName, $rel, [IO.Compression.CompressionLevel]::Optimal)
  }
} finally { $zip.Dispose() }
$check = [IO.Compression.ZipFile]::OpenRead($zipPath)
try { $bad = @($check.Entries | Where-Object { $_.FullName.Contains("\") }) } finally { $check.Dispose() }
if ($bad.Count -gt 0) { Fail "The zip has backslash entry names; Amplify would serve a blank page. Nothing was uploaded." }

# ---------------------------------------------------------------------------------------------- 4. deploy
Say "== 4. Upload and start the deployment (app $AppId, branch $Branch, region $Region)"
$created = & aws amplify create-deployment --region $Region --app-id $AppId --branch-name $Branch --output json @profileArgs | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $created.jobId) { Fail "create-deployment failed. Does the app and branch exist, and are you signed in?" }
# The upload URL is a short-lived secret: it is never printed.
Invoke-WebRequest -Uri $created.zipUploadUrl -Method Put -InFile $zipPath -ContentType "application/zip" -UseBasicParsing | Out-Null
& aws amplify start-deployment --region $Region --app-id $AppId --branch-name $Branch --job-id $created.jobId --output json @profileArgs | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "start-deployment failed." }
$status = ""
for ($i = 0; $i -lt 60; $i++) {
  $status = (& aws amplify get-job --region $Region --app-id $AppId --branch-name $Branch --job-id $created.jobId --query "job.summary.status" --output text @profileArgs).Trim()
  Say "   job $($created.jobId): $status"
  if ($status -in @("SUCCEED", "FAILED", "CANCELLED")) { break }
  Start-Sleep -Seconds 4
}
Remove-Item $zipPath -ErrorAction SilentlyContinue
if ($status -ne "SUCCEED") { Fail "The deployment did not succeed (status $status)." }

# ---------------------------------------------------------------------------------------------- 5. verify what the internet serves
$domain = (& aws amplify get-app --region $Region --app-id $AppId --query "app.defaultDomain" --output text @profileArgs).Trim()
$site = "https://$Branch.$domain"
Say "== 5. Verify $site"
$html = (Invoke-WebRequest -Uri "$site/" -UseBasicParsing -TimeoutSec 30).Content
if (-not $html.Contains('id="root"')) { Fail "The page does not contain the app root." }
$assets = [regex]::Matches($html, '(?:src|href)="(/[^"]+\.(?:js|css))"') | ForEach-Object { $_.Groups[1].Value }
if (@($assets).Count -eq 0) { Fail "The page references no scripts or styles." }
foreach ($asset in $assets) {
  $r = Invoke-WebRequest -Uri ($site + $asset) -UseBasicParsing -TimeoutSec 60
  if ($r.StatusCode -ne 200) { Fail "$asset returned HTTP $($r.StatusCode); the page would be blank." }
  Say "   200 $asset ($($r.RawContentLength) bytes)"
}
Say ""
Say "Published: $site"
Say "Check it: python scripts\smoke.py --api-url $ApiUrl --origin $site --frontend-url $site --expect-commit <deployed commit>"
Say "The API's ALLOWED_ORIGIN must be exactly $site (see docs/deployment.md)."
