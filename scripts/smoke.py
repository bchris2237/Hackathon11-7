"""Live smoke test for a DEPLOYED TradeMind API (B6). Standard library only: no install needed.

  python scripts/smoke.py --api-url https://<id>.execute-api.<region>.amazonaws.com \\
      --expect-commit <git sha you deployed> --origin https://<branch>.<appid>.amplifyapp.com \\
      [--frontend-url https://<branch>.<appid>.amplifyapp.com] [--skip-model]

What it proves, in order (each line is PASS, FAIL, INFO or SKIPPED):
  health (commit matches, mode is live) / CORS (allowed origin accepted, others not, no credentials header) /
  consistent error envelope / extract (real Bedrock) / confirm / optimize (canonical figures from the frozen contract) /
  tamper-checked explain (real Bedrock) / text proposal (real Bedrock) / explicit apply (no model) / base scenario unchanged /
  invalid input rejected / (optional) hosted frontend: no credentials in the bundle and the SPA rewrite works.

Exit code: 0 = everything ran and passed. 1 = at least one FAIL. 4 = no failure but something was SKIPPED (for example
--skip-model or no --origin): that is a PARTIAL result and must not be reported as full live verification.

The output is meant to be pasted into docs/compliance/B6.md: it prints status codes, timings and AWS/API request ids
(X-Request-Id), never request bodies, descriptions, headers other than a few named ones, or credentials. It makes real calls
that cost money (a few small model calls) and only against the URL you pass. Fictional fixture data only.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
EVIL_ORIGIN = "https://evil.example"
MAX_ASSET_BYTES = 5 * 1024 * 1024

# Expected outcomes for the canonical fixture, from the frozen contract (GENERAL-GUIDELINES.md, PART E).
# These are assertions about the REAL optimizer's answer, not values this script computes.
EXPECTED_CANONICAL_TOTALS = {
    "baselineMinutes": 1440,
    "productionMinutes": 540,
    "coordinationMinutes": 0,
    "effortMinutes": 540,
    "grossSavingsMinutes": 900,
    "netSavingsMinutes": 900,
}
DESIGN_600 = {"kind": "estimate", "businessId": "designco", "service": "design", "minutes": 600}
CREDENTIAL_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ASIA[0-9A-Z]{16}"),
    re.compile(r"aws_secret_access_key", re.IGNORECASE),
    re.compile(r"X-Amz-Security-Token", re.IGNORECASE),
    re.compile(r"secretAccessKey", re.IGNORECASE),
    re.compile(r"BEGIN (RSA |EC )?PRIVATE KEY"),
    re.compile(r"VITE_AWS", re.IGNORECASE),
]


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str = "") -> bool:
        self.rows.append((status, name, detail))
        print(f"{status:<8} {name}" + (f"  |  {detail}" if detail else ""), flush=True)
        return status != "FAIL"

    def count(self, status: str) -> int:
        return sum(1 for s, _, _ in self.rows if s == status)


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes, ms: float) -> None:
        self.status, self.headers, self.body, self.ms = status, headers, body, ms

    @property
    def request_id(self) -> str:
        return self.headers.get("x-request-id", "none")

    def json(self) -> Any:
        try:
            return json.loads(self.body or b"null")
        except ValueError:
            return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def call(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None, timeout: float = 40.0) -> Response:
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
    request_headers = {"User-Agent": "trademind-smoke/1"}
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    opener = urllib.request.build_opener(_NoRedirect)
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as resp:
            raw, status, hdrs = resp.read(MAX_ASSET_BYTES + 1), resp.status, dict(resp.headers.items())
    except urllib.error.HTTPError as err:
        raw, status, hdrs = err.read(MAX_ASSET_BYTES + 1), err.code, dict(err.headers.items())
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise ConnectionError(f"{method} {urllib.parse.urlsplit(url).path or '/'}: {type(err).__name__}") from None
    return Response(status, {k.lower(): v for k, v in hdrs.items()}, raw, (time.monotonic() - started) * 1000)


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def error_code(response: Response) -> str | None:
    body = response.json()
    return body.get("error", {}).get("code") if isinstance(body, dict) and isinstance(body.get("error"), dict) else None


def envelope_ok(response: Response) -> bool:
    body = response.json()
    err = body.get("error") if isinstance(body, dict) else None
    return (
        isinstance(err, dict)
        and set(err) == {"code", "message", "fields", "requestId"}
        and isinstance(err["fields"], list)
        and err["requestId"] == response.request_id
    )


# --------------------------------------------------------------------------- the steps
def run(args: argparse.Namespace, report: Report) -> None:
    api = args.api_url.rstrip("/")
    canonical = fixture("canonical.json")
    original = copy.deepcopy(canonical)

    def step(name: str, fn) -> Any:
        try:
            return fn()
        except ConnectionError as err:
            report.add("FAIL", name, str(err))
        except Exception as err:  # noqa: BLE001 - a smoke test must report, not crash
            report.add("FAIL", name, f"unexpected {type(err).__name__}")
        return None

    # 1. health ------------------------------------------------------------------------------------------------
    def health() -> None:
        r = call("GET", f"{api}/health")
        body = r.json() or {}
        ok = r.status == 200 and body.get("status") == "ok" and body.get("schemaVersion") == "1"
        detail = f"HTTP {r.status}, {r.ms:.0f} ms, request {r.request_id}, commit {body.get('commit')}, mode {body.get('mode')}"
        if not ok:
            report.add("FAIL", "health responds with the v1 shape", detail)
            return
        report.add("PASS", "health responds with the v1 shape", detail)
        report.add("PASS" if body.get("mode") == "live" else "FAIL", "deployed mode is live (not fixture)", f"mode={body.get('mode')}")
        if args.expect_commit:
            same = body.get("commit") == args.expect_commit
            report.add("PASS" if same else "FAIL", "deployed commit equals the commit you deployed", f"deployed={body.get('commit')} expected={args.expect_commit}")
        else:
            report.add("SKIPPED", "deployed commit equals the commit you deployed", "no --expect-commit given")

    step("health", health)

    # 2. CORS --------------------------------------------------------------------------------------------------
    def cors() -> None:
        if not args.origin:
            report.add("SKIPPED", "CORS allows the hosted frontend origin", "no --origin given")
            return
        pre = call("OPTIONS", f"{api}/optimize", headers={"Origin": args.origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
        ok = (
            pre.status == 204
            and pre.headers.get("access-control-allow-origin") == args.origin
            and "POST" in pre.headers.get("access-control-allow-methods", "")
            and "content-type" in pre.headers.get("access-control-allow-headers", "").lower()
            and "access-control-allow-credentials" not in pre.headers
        )
        report.add("PASS" if ok else "FAIL", "CORS preflight accepts the hosted origin, no credentials header", f"HTTP {pre.status}, allow-origin={pre.headers.get('access-control-allow-origin')}, request {pre.request_id}")
        evil = call("OPTIONS", f"{api}/optimize", headers={"Origin": EVIL_ORIGIN, "Access-Control-Request-Method": "POST"})
        report.add("PASS" if "access-control-allow-origin" not in evil.headers else "FAIL", "CORS preflight refuses an unlisted origin", f"HTTP {evil.status}, allow-origin={evil.headers.get('access-control-allow-origin')}")
        real = call("GET", f"{api}/health", headers={"Origin": args.origin})
        report.add("PASS" if real.headers.get("access-control-allow-origin") == args.origin and "access-control-allow-credentials" not in real.headers else "FAIL", "actual responses carry the origin header", f"allow-origin={real.headers.get('access-control-allow-origin')}")
        wild = call("GET", f"{api}/health", headers={"Origin": EVIL_ORIGIN})
        report.add("PASS" if wild.headers.get("access-control-allow-origin") not in ("*", EVIL_ORIGIN) else "FAIL", "no wildcard or reflected origin", f"allow-origin={wild.headers.get('access-control-allow-origin')}")

    step("cors", cors)

    # 3. error envelope through the gateway ---------------------------------------------------------------------
    def envelope() -> None:
        r404 = call("GET", f"{api}/definitely-not-a-route")
        report.add("PASS" if r404.status == 404 and envelope_ok(r404) and error_code(r404) == "NOT_FOUND" else "FAIL", "unknown route returns our error envelope (not the gateway's)", f"HTTP {r404.status}, code {error_code(r404)}, request {r404.request_id}")
        bad = call("POST", f"{api}/optimize", body=b"{not json")
        report.add("PASS" if bad.status == 400 and envelope_ok(bad) and error_code(bad) == "MALFORMED_JSON" else "FAIL", "malformed JSON is 400 MALFORMED_JSON", f"HTTP {bad.status}, code {error_code(bad)}, request {bad.request_id}")
        big = call("POST", f"{api}/optimize", body=b"x" * (33 * 1024))
        report.add("PASS" if big.status == 413 and error_code(big) == "PAYLOAD_TOO_LARGE" else "FAIL", "over 32 KiB is 413", f"HTTP {big.status}, code {error_code(big)}")
        missing = copy.deepcopy(canonical)
        missing["businesses"][0]["estimates"] = []
        r422 = call("POST", f"{api}/confirm", body={"scenario": missing})
        report.add("PASS" if r422.status == 422 and error_code(r422) == "MISSING_BASELINE" else "FAIL", "missing baseline is 422 MISSING_BASELINE", f"HTTP {r422.status}, code {error_code(r422)}")
        # B8 hardening. Both are refused before any work is done, so they cost nothing.
        hostile = call("POST", f"{api}/confirm", body={"scenario": canonical}, headers={"Origin": EVIL_ORIGIN})
        report.add("PASS" if hostile.status == 403 and error_code(hostile) == "ORIGIN_NOT_ALLOWED" and "access-control-allow-origin" not in hostile.headers else "FAIL", "a POST from an unlisted origin is refused with 403", f"HTTP {hostile.status}, code {error_code(hostile)}, request {hostile.request_id}")
        plain = call("POST", f"{api}/confirm", body=json.dumps({"scenario": canonical}).encode(), headers={"Content-Type": "text/plain"})
        report.add("PASS" if plain.status == 415 and error_code(plain) == "UNSUPPORTED_MEDIA_TYPE" else "FAIL", "a text/plain body is refused with 415", f"HTTP {plain.status}, code {error_code(plain)}")
        hardened = call("GET", f"{api}/health")
        needed = {"x-content-type-options": "nosniff", "cache-control": "no-store", "x-frame-options": "DENY", "referrer-policy": "no-referrer"}
        missing = [k for k, v in needed.items() if hardened.headers.get(k) != v]
        report.add("PASS" if not missing and "default-src 'none'" in hardened.headers.get("content-security-policy", "") else "FAIL", "security headers are present", f"missing={missing}")

    step("error envelope", envelope)

    # 4. extract (real Bedrock) ---------------------------------------------------------------------------------
    if args.skip_model:
        report.add("SKIPPED", "extract with real Bedrock", "--skip-model")
    else:
        def extract() -> None:
            fx = fixture("extraction.json")
            r = call("POST", f"{api}/extract", body=fx["request"], timeout=60)
            body = r.json() or {}
            draft = body.get("draft", {}) if isinstance(body, dict) else {}
            ok = (
                r.status == 200
                and body.get("mode") == "live"
                and [b.get("id") for b in draft.get("businesses", [])] == [d["id"] for d in fx["request"]["descriptions"]]
                and "confirmed" not in json.dumps(draft)
            )
            report.add("PASS" if ok else "FAIL", "extract returns a live, unconfirmed draft", f"HTTP {r.status}, {r.ms:.0f} ms, request {r.request_id}, code {error_code(r)}")
            if ok:
                want = fx["response"]["draft"]
                same = draft == want and body.get("missingFields") == fx["response"]["missingFields"]
                report.add("INFO", "extracted draft equals the fixture's expected draft", "identical" if same else f"differs (a real model may drop values; missingFields={body.get('missingFields')})")
                completed = copy.deepcopy(draft)
                for b in completed.get("businesses", []):
                    for key in ("capacityMinutes", "coordinationMinutes"):
                        if b.get(key) is None:
                            b[key] = 0  # a human states the missing value; the smoke test states 0
                c = call("POST", f"{api}/confirm", body={"scenario": {**completed, "schemaVersion": "1", "confirmed": True}})
                report.add("INFO", "extracted draft, completed by a human, passes /confirm", f"HTTP {c.status}, code {error_code(c)}")

        step("extract", extract)

    # 5. confirm ------------------------------------------------------------------------------------------------
    def confirm() -> None:
        r = call("POST", f"{api}/confirm", body={"scenario": canonical})
        report.add("PASS" if r.status == 200 and r.json() == {"scenario": canonical} else "FAIL", "confirm round-trips the canonical scenario unchanged", f"HTTP {r.status}, request {r.request_id}")

    step("confirm", confirm)

    # 6. optimize -----------------------------------------------------------------------------------------------
    optimized: dict[str, Any] = {}

    def optimize() -> None:
        r = call("POST", f"{api}/optimize", body={"scenario": canonical})
        body = r.json() or {}
        result = body.get("result") if isinstance(body, dict) else None
        if r.status != 200 or not isinstance(result, dict):
            report.add("FAIL", "optimize returns a result", f"HTTP {r.status}, code {error_code(r)}, request {r.request_id}")
            return
        optimized.update(result)
        report.add("PASS" if body.get("mode") == "live" else "FAIL", "optimize is labelled live", f"mode={body.get('mode')}, {r.ms:.0f} ms, request {r.request_id}")
        report.add("PASS" if result.get("status") == "collaboration" else "FAIL", "canonical scenario is a collaboration", f"status={result.get('status')}")
        totals = result.get("totals") or {}
        report.add("PASS" if totals == EXPECTED_CANONICAL_TOTALS else "FAIL", "canonical totals match the frozen contract (1440 / 540 / 900)", f"got {totals}")
        saved = [m.get("netSavingsMinutes") for m in (result.get("businessMetrics") or [])]
        report.add("PASS" if saved == [300, 300, 300] else "FAIL", "each business saves 300 minutes", f"got {saved}")
        cert = result.get("certificate") or {}
        shape = cert.get("verification") == "valid" and all(re.fullmatch(r"[0-9a-f]{64}", str(cert.get(k, ""))) for k in ("scenarioDigest", "allocationDigest"))
        report.add("PASS" if shape else "FAIL", "certificate is present and well formed", f"verification={cert.get('verification')}")
        mine = canonical_digest(canonical)
        report.add("INFO", "certificate scenarioDigest equals this script's canonical-JSON SHA-256 of the scenario", "match" if cert.get("scenarioDigest") == mine else "differs (check which object the contract digests; not a failure by itself)")

    step("optimize", optimize)

    # 7. explain (real Bedrock) + tamper ------------------------------------------------------------------------
    if optimized and not args.skip_model:
        def explain() -> None:
            r = call("POST", f"{api}/explain", body={"scenario": canonical, "result": optimized}, timeout=60)
            body = r.json() or {}
            ok = r.status == 200 and body.get("mode") == "live" and isinstance(body.get("text"), str) and body["text"].strip() != "" and set(body) == {"text", "mode"}
            report.add("PASS" if ok else "FAIL", "explain returns grounded text from real Bedrock", f"HTTP {r.status}, {r.ms:.0f} ms, request {r.request_id}, code {error_code(r)}, {len(body.get('text', ''))} chars")
            forged = copy.deepcopy(optimized)
            forged["totals"]["grossSavingsMinutes"] = 5000
            t = call("POST", f"{api}/explain", body={"scenario": canonical, "result": forged})
            err = (t.json() or {}).get("error", {}) if isinstance(t.json(), dict) else {}
            report.add("PASS" if t.status == 422 and err.get("code") == "RESULT_MISMATCH" and "result.totals.grossSavingsMinutes" in err.get("fields", []) else "FAIL", "tampered totals are refused before the model is used", f"HTTP {t.status}, code {err.get('code')}, request {t.request_id}")

        step("explain", explain)
    else:
        report.add("SKIPPED", "explain with real Bedrock and tamper refusal", "--skip-model" if args.skip_model else "optimize did not return a result")

    # 8-9. proposal then apply ----------------------------------------------------------------------------------
    proposed: list[dict[str, Any]] | None = None
    if args.skip_model:
        report.add("SKIPPED", "text proposal with real Bedrock", "--skip-model")
        proposed = [DESIGN_600]
    else:
        def propose() -> None:
            nonlocal proposed
            text = "DesignCo's design time is now 10 hours."
            r = call("POST", f"{api}/simulate", body={"scenario": canonical, "changes": [], "text": text, "apply": False}, timeout=60)
            body = r.json() or {}
            ok = (
                r.status == 200
                and body.get("requiresConfirmation") is True
                and body.get("result") is None
                and body.get("scenario") == canonical
                and body.get("mode") == "live"
            )
            report.add("PASS" if ok else "FAIL", "text becomes a PROPOSAL only: original scenario returned, nothing computed", f"HTTP {r.status}, {r.ms:.0f} ms, request {r.request_id}, code {error_code(r)}")
            if ok:
                proposed = body.get("proposedChanges")
                report.add("PASS" if proposed == [DESIGN_600] else "FAIL", "the proposed change is DesignCo design = 600 minutes", f"got {proposed}")
            mixed = call("POST", f"{api}/simulate", body={"scenario": canonical, "changes": [DESIGN_600], "text": text, "apply": False})
            report.add("PASS" if mixed.status == 422 else "FAIL", "text plus changes is rejected", f"HTTP {mixed.status}")
            applied = call("POST", f"{api}/simulate", body={"scenario": canonical, "changes": [], "text": text, "apply": True})
            report.add("PASS" if applied.status == 422 else "FAIL", "text with apply=true is rejected", f"HTTP {applied.status}")

        step("propose", propose)

    if proposed:
        def apply() -> None:
            r = call("POST", f"{api}/simulate", body={"scenario": canonical, "changes": proposed, "apply": True}, timeout=60)
            body = r.json() or {}
            result = body.get("result") if isinstance(body, dict) else None
            ok = r.status == 200 and isinstance(result, dict) and body.get("requiresConfirmation") is False
            report.add("PASS" if ok else "FAIL", "explicit apply recomputes with the deterministic engine", f"HTTP {r.status}, {r.ms:.0f} ms, request {r.request_id}, code {error_code(r)}")
            if not ok:
                return
            report.add("PASS" if result.get("status") == "no_exchange" else "FAIL", "DesignCo design = 600 gives no_exchange (not infeasible)", f"status={result.get('status')}")
            new_cert = (result.get("certificate") or {}).get("scenarioDigest")
            old_cert = (optimized.get("certificate") or {}).get("scenarioDigest") if optimized else None
            report.add("PASS" if new_cert and new_cert != old_cert else "FAIL", "recalculated certificate is for the CHANGED scenario", f"new digest differs from the original's: {bool(new_cert and new_cert != old_cert)}")
            changed = next(b for b in body["scenario"]["businesses"] if b["id"] == "designco")
            report.add("PASS" if {e["service"]: e["minutes"] for e in changed["estimates"]}.get("design") == 600 else "FAIL", "returned scenario carries the change", "")

        step("apply", apply)
    else:
        report.add("SKIPPED", "explicit apply", "no usable proposal")

    report.add("PASS" if canonical == original else "FAIL", "the base scenario was never modified by this run", "")

    # 9b. optional profile storage (B7) -------------------------------------------------------------------------
    if args.profiles:
        step("profiles", lambda: check_profiles(api, canonical, report))
    else:
        report.add("INFO", "profile storage was not checked", "optional; pass --profiles against a stack deployed with -EnableProfiles")

    # 10. hosted frontend ---------------------------------------------------------------------------------------
    if args.frontend_url:
        step("frontend", lambda: check_frontend(args.frontend_url.rstrip("/"), api, report))
    else:
        report.add("SKIPPED", "hosted frontend: no credentials in the bundle, SPA rewrite works", "no --frontend-url given")


def check_profiles(api: str, canonical: dict[str, Any], report: Report) -> None:
    """Optional B7 steps. Stores ONE fictional fixture under a random id; it expires by itself (the table's TTL)."""
    import secrets

    pid = secrets.token_urlsafe(24)  # 32 URL-safe characters from a cryptographic source; never printed
    url = f"{api}/profiles/{pid}"
    put = call("PUT", url, body={"scenario": canonical})
    stored = put.json() or {}
    ok = put.status == 200 and isinstance(stored, dict) and stored.get("id") == pid and stored.get("scenario") == canonical
    report.add("PASS" if ok else "FAIL", "profile PUT stores the fictional canonical scenario", f"HTTP {put.status}, {put.ms:.0f} ms, request {put.request_id}, code {error_code(put)}")
    if not ok:
        return
    got = call("GET", url)
    body = got.json() or {}
    report.add("PASS" if got.status == 200 and isinstance(body, dict) and body.get("scenario") == canonical else "FAIL", "profile GET returns exactly what was stored", f"HTTP {got.status}, {got.ms:.0f} ms, request {got.request_id}")
    again = call("PUT", url, body={"scenario": canonical})
    report.add("PASS" if again.status == 409 and error_code(again) == "PROFILE_EXISTS" else "FAIL", "a second PUT to the same id is refused with 409", f"HTTP {again.status}, code {error_code(again)}")
    unknown = call("GET", f"{api}/profiles/{secrets.token_urlsafe(24)}")
    report.add("PASS" if unknown.status == 404 and error_code(unknown) == "PROFILE_NOT_FOUND" else "FAIL", "an unknown id is 404", f"HTTP {unknown.status}, code {error_code(unknown)}")
    weak = call("PUT", f"{api}/profiles/{'a' * 40}", body={"scenario": canonical})
    report.add("PASS" if weak.status == 422 and error_code(weak) == "VALIDATION_ERROR" else "FAIL", "a weak id is refused with 422", f"HTTP {weak.status}, code {error_code(weak)}")
    listing = call("GET", f"{api}/profiles")
    report.add("PASS" if listing.status in (404, 405) else "FAIL", "profile route is not a listing endpoint", f"HTTP {listing.status}")


def check_frontend(site: str, api: str, report: Report) -> None:
    index = call("GET", site + "/")
    html = index.body.decode("utf-8", "replace")
    report.add("PASS" if index.status == 200 and 'id="root"' in html else "FAIL", "frontend index loads", f"HTTP {index.status}, {index.ms:.0f} ms")
    assets = re.findall(r'(?:src|href)="(/[^"]+\.(?:js|css))"', html)[:10]
    leaks: list[str] = []
    mentions_api = False
    for path in assets:
        asset = call("GET", site + path)
        text = asset.body.decode("utf-8", "replace")
        leaks += [f"{path}:{p.pattern}" for p in CREDENTIAL_PATTERNS if p.search(text)]
        mentions_api = mentions_api or api in text
    report.add("PASS" if assets and not leaks else "FAIL", "no credential patterns in the shipped bundle", f"{len(assets)} asset(s) scanned" + (f", FOUND {leaks}" if leaks else ""))
    report.add("INFO", "bundle is built for this API URL", "yes" if mentions_api else "API URL not found in the bundle (check VITE_API_BASE_URL in Amplify and rebuild)")
    deep = call("GET", site + "/some/deep/link")
    report.add("PASS" if deep.status == 200 and 'id="root"' in deep.body.decode("utf-8", "replace") else "FAIL", "SPA rewrite: a deep link serves the app, not a 404", f"HTTP {deep.status}")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Live smoke test for a deployed TradeMind API.")
    p.add_argument("--api-url", required=True, help="API base URL from the stack output ApiUrl")
    p.add_argument("--expect-commit", help="git commit id you deployed (compared with GET /health)")
    p.add_argument("--origin", help="the hosted frontend origin that CORS must allow")
    p.add_argument("--frontend-url", help="hosted frontend URL to scan for credentials and test the SPA rewrite")
    p.add_argument("--profiles", action="store_true", help="also test optional profile storage (needs a stack deployed with -EnableProfiles)")
    p.add_argument("--skip-model", action="store_true", help="skip steps that call Bedrock (result is PARTIAL, exit code 4)")
    args = p.parse_args(argv)
    parts = urllib.parse.urlsplit(args.api_url)
    local = parts.hostname in ("127.0.0.1", "localhost")
    if parts.scheme != "https" and not local:
        p.error("--api-url must be https (plain http is accepted only for 127.0.0.1/localhost)")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = Report()
    print(f"TradeMind live smoke test\n  api      : {args.api_url}\n  origin   : {args.origin or '(not given)'}\n  commit   : {args.expect_commit or '(not given)'}\n")
    run(args, report)
    print(f"\nsummary: {report.count('PASS')} passed, {report.count('FAIL')} failed, {report.count('SKIPPED')} skipped, {report.count('INFO')} info")
    if report.count("FAIL"):
        print("RESULT: FAIL")
        return 1
    if report.count("SKIPPED"):
        print("RESULT: PARTIAL (something was skipped: this is NOT full live verification)")
        return 4
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
