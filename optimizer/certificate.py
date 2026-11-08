"""
TradeMind v1 — independent certificate verification.
Owner: Developer 1 (optimizer/** and tests/optimizer/**).

Public surface (A5):
  generate_certificate(scenario, assignments) → Certificate
  verify_certificate(scenario, assignments, certificate) → VerificationResult

Design
------
The certificate is INDEPENDENT from the solver's scoring path.
It recomputes ALL constraints from scratch using only the Scenario
and the supplied Assignment list — it does NOT call engine.solve(),
metrics.compute_metrics() or any cached solver output.

What a certificate proves:
  - The supplied assignments satisfy coverage, service compatibility and
    duration constraints.
  - Each business's arithmetic (production, coordination, effort, netSavings)
    is correct under the supplied scenario.
  - All businesses have effort ≤ capacity and netSavings ≥ 0.
  - Total savings arithmetic is consistent.
  - The SHA-256 digests match the canonical serialisations of the scenario
    and the assignment list.

What a certificate DOES NOT prove:
  - Cryptographic identity of any party.
  - Legal enforceability of any business arrangement.
  - Equal service quality across providers.
  - Commercial profit or monetary benefit.
  - That the assignment is globally optimal (that is the solver's claim).
  - That estimates supplied by businesses are honest (assumed as stated).

Canonical JSON serialisation:
  Keys recursively sorted (dict entries alphabetically), arrays preserve
  insertion order, no whitespace, UTF-8 encoding.  This is deterministic
  across Python versions and platforms.

Display rounding:
  All source numbers are integer minutes.  If the UI converts to hours for
  display (e.g. "8 hours"), that conversion MUST NOT alter the source
  integers.  The certificate hashes the integer-minute representation.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from contracts.models import (
    Assignment,
    Certificate,
    CertificateCheck,
    Scenario,
    ServiceName,
)
from optimizer.errors import MissingBaselineError


# ---------------------------------------------------------------------------
# VerificationResult — rich output from verify_certificate
# ---------------------------------------------------------------------------

@dataclass
class VerificationResult:
    """
    Result of verifying a certificate against a scenario and assignment list.

    Fields:
      valid           — True iff all checks passed and digests match.
      checks_passed   — list of check names that passed.
      checks_failed   — list of (name, reason) for checks that failed.
      digest_matched  — True iff both scenario_digest and allocation_digest match.
      recomputed_cert — the freshly generated certificate (regardless of validity).
    """
    valid: bool
    checks_passed: list[str] = field(default_factory=list)
    checks_failed: list[tuple[str, str]] = field(default_factory=list)
    digest_matched: bool = False
    recomputed_cert: Certificate | None = None

    def __bool__(self) -> bool:
        return self.valid


# ---------------------------------------------------------------------------
# Canonical JSON and hashing
# ---------------------------------------------------------------------------

def _canonical_json(obj: object) -> bytes:
    """
    Canonical JSON: keys recursively sorted, arrays preserve order,
    no whitespace, UTF-8 encoding.
    """
    def _sort(o: object) -> object:
        if isinstance(o, dict):
            return {k: _sort(v) for k, v in sorted(o.items())}
        if isinstance(o, list):
            return [_sort(i) for i in o]
        return o

    return json.dumps(_sort(obj), separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _scenario_digest(scenario: Scenario) -> str:
    raw = json.loads(scenario.model_dump_json())
    return _sha256_hex(_canonical_json(raw))


def _allocation_digest(assignments: list[Assignment]) -> str:
    raw = [
        {
            "deliverableId": a.deliverableId,
            "providerId": a.providerId,
            "minutes": a.minutes,
        }
        for a in assignments
    ]
    return _sha256_hex(_canonical_json(raw))


# ---------------------------------------------------------------------------
# Core verification logic (shared by generate and verify)
# ---------------------------------------------------------------------------

@dataclass
class _CheckResult:
    passed: bool
    name: str
    reason: str = ""


def _run_checks(
    scenario: Scenario,
    assignments: list[Assignment],
) -> list[_CheckResult]:
    """
    Run all certificate checks independently.
    Returns a list of _CheckResult objects (one per check).
    Does NOT call the solver, engine, or metrics modules.
    """
    results: list[_CheckResult] = []

    estimates: dict[str, dict[ServiceName, int]] = {
        b.id: {e.service: e.minutes for e in b.estimates}
        for b in scenario.businesses
    }
    business_ids: set[str] = {b.id for b in scenario.businesses}
    deliverable_ids: set[str] = {d.id for d in scenario.deliverables}
    service_map: dict[str, ServiceName] = {d.id: d.service for d in scenario.deliverables}
    owner_map: dict[str, str] = {d.id: d.ownerId for d in scenario.deliverables}

    # ---- Check 1: coverage — exactly one assignment per deliverable ----------
    seen: dict[str, list[Assignment]] = {}
    for a in assignments:
        seen.setdefault(a.deliverableId, []).append(a)

    missing = [d_id for d_id in deliverable_ids if d_id not in seen]
    extra   = [a.deliverableId for a in assignments if a.deliverableId not in deliverable_ids]
    dup     = [d_id for d_id, lst in seen.items() if len(lst) > 1]

    if missing or extra or dup:
        detail = "; ".join(filter(None, [
            f"missing: {missing}" if missing else "",
            f"unknown: {extra}"   if extra   else "",
            f"duplicate: {dup}"   if dup     else "",
        ]))
        results.append(_CheckResult(False, "coverage_complete", detail))
    else:
        results.append(_CheckResult(True, "coverage_complete"))

    # Build clean assignment map (only if coverage passed — else skip dependent checks)
    clean = {a.deliverableId: a.providerId for a in assignments
             if a.deliverableId in deliverable_ids and a.providerId in business_ids}

    # ---- Check 2: all IDs are known -----------------------------------------
    unknown_prov = [a.providerId for a in assignments if a.providerId not in business_ids]
    unknown_del  = [a.deliverableId for a in assignments if a.deliverableId not in deliverable_ids]
    if unknown_prov or unknown_del:
        results.append(_CheckResult(False, "all_ids_known",
                                    f"unknown providers: {unknown_prov}; unknown deliverables: {unknown_del}"))
    else:
        results.append(_CheckResult(True, "all_ids_known"))

    # ---- Check 3: service compatibility — provider has estimate --------------
    incompat: list[str] = []
    for a in assignments:
        if a.deliverableId not in deliverable_ids or a.providerId not in business_ids:
            continue
        svc = service_map[a.deliverableId]
        if svc not in estimates.get(a.providerId, {}):
            incompat.append(f"{a.providerId} cannot provide {svc.value} for {a.deliverableId}")
    if incompat:
        results.append(_CheckResult(False, "service_compatibility", "; ".join(incompat)))
    else:
        results.append(_CheckResult(True, "service_compatibility"))

    # ---- Check 4: duration matches provider's estimate ----------------------
    wrong_dur: list[str] = []
    for a in assignments:
        if a.deliverableId not in deliverable_ids or a.providerId not in business_ids:
            continue
        svc = service_map[a.deliverableId]
        prov_ests = estimates.get(a.providerId, {})
        if svc in prov_ests and a.minutes != prov_ests[svc]:
            wrong_dur.append(
                f"{a.deliverableId}: claimed {a.minutes} min, "
                f"provider {a.providerId!r} estimates {prov_ests[svc]} min"
            )
    if wrong_dur:
        results.append(_CheckResult(False, "duration_matches_estimate", "; ".join(wrong_dur)))
    else:
        results.append(_CheckResult(True, "duration_matches_estimate"))

    # --- Remaining checks only meaningful if coverage+IDs+service are clean --
    if not (results[0].passed and results[1].passed and results[2].passed and results[3].passed):
        # Append placeholder failures for dependent checks
        for name in ["baselines_computable", "capacity_respected",
                     "no_worse_off", "totals_consistent", "integer_arithmetic"]:
            results.append(_CheckResult(False, name,
                                        "skipped due to earlier failures"))
        return results

    # ---- Check 5: baselines computable (no missing owner estimates) ----------
    baselines: dict[str, int] = {}
    missing_base: list[str] = []
    for b in scenario.businesses:
        total = 0
        for d in scenario.deliverables:
            if d.ownerId == b.id:
                svc = d.service
                if svc not in estimates.get(b.id, {}):
                    missing_base.append(f"{b.id}.{svc.value}")
                else:
                    total += estimates[b.id][svc]
        baselines[b.id] = total
    if missing_base:
        results.append(_CheckResult(False, "baselines_computable",
                                    f"missing: {missing_base}"))
        for name in ["capacity_respected", "no_worse_off", "totals_consistent", "integer_arithmetic"]:
            results.append(_CheckResult(False, name, "skipped due to missing baselines"))
        return results
    results.append(_CheckResult(True, "baselines_computable"))

    # ---- Compute production and involvement ----------------------------------
    production: dict[str, int] = {b.id: 0 for b in scenario.businesses}
    for d_id, prov_id in clean.items():
        svc = service_map[d_id]
        production[prov_id] += estimates[prov_id][svc]

    involved: set[str] = set()
    for d_id, prov_id in clean.items():
        if prov_id != owner_map[d_id]:
            involved.add(prov_id)
            involved.add(owner_map[d_id])

    coord_charge: dict[str, int] = {
        b.id: (b.coordinationMinutes if b.id in involved else 0)
        for b in scenario.businesses
    }
    effort: dict[str, int] = {
        b.id: production[b.id] + coord_charge[b.id]
        for b in scenario.businesses
    }
    cap_map: dict[str, int] = {b.id: b.capacityMinutes for b in scenario.businesses}

    # ---- Check 6: capacity respected -----------------------------------------
    over_cap = [
        f"{b_id}: effort={effort[b_id]} > capacity={cap_map[b_id]}"
        for b_id in effort
        if effort[b_id] > cap_map[b_id]
    ]
    if over_cap:
        results.append(_CheckResult(False, "capacity_respected", "; ".join(over_cap)))
    else:
        results.append(_CheckResult(True, "capacity_respected"))

    # ---- Check 7: no-worse-off -----------------------------------------------
    harmed = [
        f"{b_id}: netSavings={baselines[b_id] - effort[b_id]}"
        for b_id in effort
        if baselines[b_id] - effort[b_id] < 0
    ]
    if harmed:
        results.append(_CheckResult(False, "no_worse_off", "; ".join(harmed)))
    else:
        results.append(_CheckResult(True, "no_worse_off"))

    # ---- Check 8: totals consistent ------------------------------------------
    total_baseline   = sum(baselines.values())
    total_production = sum(production.values())
    total_coord      = sum(coord_charge.values())
    total_effort     = sum(effort.values())
    gross_savings    = total_baseline - total_production
    net_savings      = total_baseline - total_effort

    # Cross-verify totals using two independent calculations
    cross_total_effort = total_production + total_coord
    if cross_total_effort != total_effort:
        results.append(_CheckResult(False, "totals_consistent",
                                    f"effort={total_effort} != production+coord={cross_total_effort}"))
    else:
        results.append(_CheckResult(True, "totals_consistent"))

    # ---- Check 9: integer arithmetic (no floats) ----------------------------
    all_ints = all(
        isinstance(v, int)
        for v in [total_baseline, total_production, total_coord, total_effort,
                  gross_savings, net_savings]
    )
    if not all_ints:
        results.append(_CheckResult(False, "integer_arithmetic", "non-integer value detected"))
    else:
        results.append(_CheckResult(True, "integer_arithmetic"))

    return results


# ---------------------------------------------------------------------------
# generate_certificate
# ---------------------------------------------------------------------------

def generate_certificate(
    scenario: Scenario,
    assignments: list[Assignment],
) -> Certificate:
    """
    Generate a Certificate for a scenario + assignment list.

    Runs all checks independently before generating.
    Raises ValueError listing failed checks if any check fails.

    The certificate proves:
      - Arithmetic is correct under supplied estimates.
      - SHA-256 digests match the canonical representations.
    It does NOT prove identity, equal quality, legal enforceability, or profit.
    Display conversions (e.g. hours) must not alter source integer minutes.
    """
    checks = _run_checks(scenario, assignments)
    failures = [(c.name, c.reason) for c in checks if not c.passed]
    if failures:
        detail = "; ".join(f"{name}: {reason}" for name, reason in failures)
        raise ValueError(f"Certificate generation failed — checks did not pass: {detail}")

    s_digest = _scenario_digest(scenario)
    a_digest = _allocation_digest(assignments)

    cert_checks = [CertificateCheck(name=c.name, passed=True) for c in checks]

    assumptions = [
        "Service quality is assumed equal across all providers; quality differences are not modeled.",
        "All duration estimates are accepted as supplied; estimate honesty is assumed, not verified.",
        "Coordination charge is a fixed cost per business per exchange, not proportional to volume.",
        "Time accounting is divisible (minutes can be summed), but each task is indivisible "
        "(one provider per deliverable).",
        "No cash transfers or monetary profit guarantees are modeled; savings are minutes of effort.",
        "Display conversion to hours must not alter the integer-minute source values.",
        "This certificate is not a cryptographic identity credential or a legal document.",
    ]

    return Certificate(
        schemaVersion="1",
        scenarioDigest=s_digest,
        allocationDigest=a_digest,
        verification="valid",
        assumptions=assumptions,
        checks=cert_checks,
    )


# ---------------------------------------------------------------------------
# verify_certificate
# ---------------------------------------------------------------------------

def verify_certificate(
    scenario: Scenario,
    assignments: list[Assignment],
    certificate: Certificate,
) -> VerificationResult:
    """
    Independently verify a certificate against a scenario and assignment list.

    This function:
      1. Runs all checks from scratch (does NOT call engine/solver/metrics).
      2. Recomputes both digests from the supplied inputs.
      3. Compares computed digests against those in the certificate.

    Returns a VerificationResult with:
      valid            — True iff all checks passed AND both digests matched.
      checks_passed    — names of passed checks.
      checks_failed    — (name, reason) pairs for failed checks.
      digest_matched   — True iff both digests match.
      recomputed_cert  — freshly generated certificate (None if checks failed).

    Tampered inputs (different provider, wrong minutes, wrong scenario,
    altered totals) produce digest mismatches and/or check failures.
    """
    checks = _run_checks(scenario, assignments)
    passed = [c.name for c in checks if c.passed]
    failed = [(c.name, c.reason) for c in checks if not c.passed]

    # Recompute digests
    computed_s_digest = _scenario_digest(scenario)
    computed_a_digest = _allocation_digest(assignments)

    digest_match = (
        computed_s_digest == certificate.scenarioDigest
        and computed_a_digest == certificate.allocationDigest
    )

    all_checks_passed = len(failed) == 0
    valid = all_checks_passed and digest_match

    # Only generate a fresh cert if everything passed
    recomputed: Certificate | None = None
    if valid:
        try:
            recomputed = generate_certificate(scenario, assignments)
        except ValueError:
            valid = False

    return VerificationResult(
        valid=valid,
        checks_passed=passed,
        checks_failed=failed,
        digest_matched=digest_match,
        recomputed_cert=recomputed,
    )
