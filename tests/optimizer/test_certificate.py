"""
A5 — Independent certificate verification tests.
Owner: Developer 1.

Tests generate_certificate and verify_certificate from optimizer/certificate.py.
All expected values come from frozen contract spec and canonical fixtures.

Required A5 acceptance cases:
  - Tamper provider → verify rejects
  - Tamper minutes → verify rejects
  - Tamper scenario digest → verify rejects
  - Tamper totals (not in certificate, but changes digest) → verify rejects
  - Tamper deliverable assignment → verify rejects
  - Modify scenario input → scenario digest differs
  - Canonical fixture verified independently
  - Coordination fixture verified independently
  - Certificate makes no profitability or cryptographic identity claims
  - generate_certificate raises on invalid input
"""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from tests.optimizer import _fixtures
from optimizer.contract import Assignment, Certificate, Scenario
from optimizer.certificate import (
    VerificationResult,
    _allocation_digest,
    _run_checks,
    _scenario_digest,
    generate_certificate,
    verify_certificate,
)
from optimizer.solver import optimize

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load(name: str) -> dict:
    return _fixtures.load(name)



def _scenario(name: str) -> Scenario:
    return Scenario.model_validate(_load(name)["scenario"])


def _make_scenario(businesses: list[dict], deliverables: list[dict]) -> Scenario:
    return Scenario.model_validate({
        "schemaVersion": "1",
        "confirmed": True,
        "businesses": businesses,
        "deliverables": deliverables,
    })


def _canonical_optimal_assignments() -> list[Assignment]:
    return [
        Assignment(deliverableId="d-design",  providerId="designco", minutes=180),
        Assignment(deliverableId="d-writing", providerId="copyco",   minutes=180),
        Assignment(deliverableId="d-coding",  providerId="devco",    minutes=180),
    ]


def _coordination_optimal_assignments() -> list[Assignment]:
    return [
        Assignment(deliverableId="d-design",  providerId="designco", minutes=180),
        Assignment(deliverableId="d-writing", providerId="copyco",   minutes=180),
        Assignment(deliverableId="d-coding",  providerId="devco",    minutes=180),
    ]


# ===========================================================================
# generate_certificate
# ===========================================================================

class TestGenerateCertificateValid:

    def test_canonical_generates_successfully(self):
        cert = generate_certificate(_scenario("canonical.json"), _canonical_optimal_assignments())
        assert isinstance(cert, Certificate)

    def test_coordination_generates_successfully(self):
        cert = generate_certificate(_scenario("coordination.json"), _coordination_optimal_assignments())
        assert isinstance(cert, Certificate)

    def test_schema_version(self):
        cert = generate_certificate(_scenario("canonical.json"), _canonical_optimal_assignments())
        assert cert.schemaVersion == "1"

    def test_verification_field(self):
        cert = generate_certificate(_scenario("canonical.json"), _canonical_optimal_assignments())
        assert cert.verification == "valid"

    def test_scenario_digest_is_64_hex_chars(self):
        cert = generate_certificate(_scenario("canonical.json"), _canonical_optimal_assignments())
        assert len(cert.scenarioDigest) == 64
        assert all(c in "0123456789abcdef" for c in cert.scenarioDigest)

    def test_allocation_digest_is_64_hex_chars(self):
        cert = generate_certificate(_scenario("canonical.json"), _canonical_optimal_assignments())
        assert len(cert.allocationDigest) == 64
        assert all(c in "0123456789abcdef" for c in cert.allocationDigest)

    def test_assumptions_present(self):
        cert = generate_certificate(_scenario("canonical.json"), _canonical_optimal_assignments())
        assert len(cert.assumptions) > 0
        assert all(isinstance(a, str) for a in cert.assumptions)

    def test_no_profitability_claim_in_assumptions(self):
        cert = generate_certificate(_scenario("canonical.json"), _canonical_optimal_assignments())
        text = " ".join(cert.assumptions).lower()
        # Must explicitly state what it does NOT prove
        assert "profit" in text or "monetary" in text or "cash" in text

    def test_no_cryptographic_identity_claim(self):
        cert = generate_certificate(_scenario("canonical.json"), _canonical_optimal_assignments())
        text = " ".join(cert.assumptions).lower()
        assert "identity" in text or "credential" in text or "legal" in text

    def test_all_checks_passed(self):
        cert = generate_certificate(_scenario("canonical.json"), _canonical_optimal_assignments())
        assert all(c.passed for c in cert.checks)

    def test_checks_names_present(self):
        cert = generate_certificate(_scenario("canonical.json"), _canonical_optimal_assignments())
        names = {c.name for c in cert.checks}
        assert "coverage_complete" in names
        assert "service_compatibility" in names
        assert "capacity_respected" in names
        assert "no_worse_off" in names

    def test_same_input_same_digests(self):
        """Determinism: same input always produces same digests."""
        cert1 = generate_certificate(_scenario("canonical.json"), _canonical_optimal_assignments())
        cert2 = generate_certificate(_scenario("canonical.json"), _canonical_optimal_assignments())
        assert cert1.scenarioDigest   == cert2.scenarioDigest
        assert cert1.allocationDigest == cert2.allocationDigest

    def test_solver_and_generate_produce_matching_digests(self):
        """
        The solver's built-in certificate and generate_certificate must produce
        the same digests for the same scenario and assignment list.
        """
        scenario = _scenario("canonical.json")
        result = optimize(scenario)
        assert result.certificate is not None

        fresh_cert = generate_certificate(scenario, result.assignments)
        assert fresh_cert.scenarioDigest   == result.certificate.scenarioDigest
        assert fresh_cert.allocationDigest == result.certificate.allocationDigest

    def test_self_assignment_generates_cert(self):
        """Self-assignment is feasible → certificate should generate."""
        self_assignments = [
            Assignment(deliverableId="d-design",  providerId="devco",    minutes=480),
            Assignment(deliverableId="d-writing", providerId="designco", minutes=480),
            Assignment(deliverableId="d-coding",  providerId="copyco",   minutes=480),
        ]
        cert = generate_certificate(_scenario("canonical.json"), self_assignments)
        assert cert.verification == "valid"


class TestGenerateCertificateInvalid:

    def test_raises_on_missing_deliverable(self):
        incomplete = [
            Assignment(deliverableId="d-design",  providerId="designco", minutes=180),
            # d-writing and d-coding omitted
        ]
        with pytest.raises(ValueError, match="coverage_complete"):
            generate_certificate(_scenario("canonical.json"), incomplete)

    def test_raises_on_incompatible_service(self):
        bad = [
            Assignment(deliverableId="d-design",  providerId="copyco",   minutes=180),  # copyco has no design
            Assignment(deliverableId="d-writing", providerId="copyco",   minutes=180),
            Assignment(deliverableId="d-coding",  providerId="devco",    minutes=180),
        ]
        with pytest.raises(ValueError):
            generate_certificate(_scenario("canonical.json"), bad)

    def test_raises_on_wrong_minutes(self):
        bad = [
            Assignment(deliverableId="d-design",  providerId="designco", minutes=999),  # should be 180
            Assignment(deliverableId="d-writing", providerId="copyco",   minutes=180),
            Assignment(deliverableId="d-coding",  providerId="devco",    minutes=180),
        ]
        with pytest.raises(ValueError, match="duration_matches_estimate"):
            generate_certificate(_scenario("canonical.json"), bad)

    def test_raises_on_capacity_exceeded(self):
        scenario = _make_scenario(
            businesses=[
                {"id": "devco",    "name": "A", "capacityMinutes": 0, "coordinationMinutes": 0,
                 "estimates": [{"service": "design", "minutes": 480}]},
                {"id": "designco", "name": "B", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "writing", "minutes": 480}]},
                {"id": "copyco",   "name": "C", "capacityMinutes": 600, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 480}]},
            ],
            deliverables=[
                {"id": "d-design",  "ownerId": "devco",    "service": "design"},
                {"id": "d-writing", "ownerId": "designco", "service": "writing"},
                {"id": "d-coding",  "ownerId": "copyco",   "service": "coding"},
            ],
        )
        # devco self-assignment: effort=480 > capacity=0
        bad = [
            Assignment(deliverableId="d-design",  providerId="devco",    minutes=480),
            Assignment(deliverableId="d-writing", providerId="designco", minutes=480),
            Assignment(deliverableId="d-coding",  providerId="copyco",   minutes=480),
        ]
        with pytest.raises(ValueError, match="capacity_respected"):
            generate_certificate(scenario, bad)

    def test_raises_on_harmed_business(self):
        """
        devco owns d-design and d-coding. Assigning both to devco (self):
        prod=480+180=660, baseline=480, net=-180 → harmed.
        """
        scenario = _make_scenario(
            businesses=[
                {"id": "devco",    "name": "A", "capacityMinutes": 9999, "coordinationMinutes": 0,
                 "estimates": [{"service": "coding", "minutes": 180}, {"service": "design", "minutes": 480}]},
                {"id": "designco", "name": "B", "capacityMinutes": 9999, "coordinationMinutes": 0,
                 "estimates": [{"service": "design", "minutes": 180}, {"service": "writing", "minutes": 480}]},
                {"id": "copyco",   "name": "C", "capacityMinutes": 9999, "coordinationMinutes": 0,
                 "estimates": [{"service": "writing", "minutes": 180}, {"service": "coding", "minutes": 480}]},
            ],
            deliverables=[
                {"id": "d-design",  "ownerId": "devco",    "service": "design"},
                {"id": "d-writing", "ownerId": "designco", "service": "writing"},
                {"id": "d-coding",  "ownerId": "copyco",   "service": "coding"},
            ],
        )
        # devco does both d-design(self,480) and d-coding(cross,180) → net=-180
        bad = [
            Assignment(deliverableId="d-design",  providerId="devco",    minutes=480),
            Assignment(deliverableId="d-writing", providerId="designco", minutes=480),
            Assignment(deliverableId="d-coding",  providerId="devco",    minutes=180),
        ]
        with pytest.raises(ValueError, match="no_worse_off"):
            generate_certificate(scenario, bad)


# ===========================================================================
# verify_certificate — tamper tests
# ===========================================================================

class TestVerifyCertificateTamperProvider:
    """A5 required: tamper provider → verify rejects."""

    def test_tampered_provider_rejected(self):
        scenario = _scenario("canonical.json")
        assignments = _canonical_optimal_assignments()
        cert = generate_certificate(scenario, assignments)

        # Tamper: change provider for d-coding from devco to designco
        # designco has no coding estimate → service_compatibility will fail
        tampered = [
            Assignment(deliverableId="d-design",  providerId="designco", minutes=180),
            Assignment(deliverableId="d-writing", providerId="copyco",   minutes=180),
            Assignment(deliverableId="d-coding",  providerId="designco", minutes=180),  # tampered
        ]
        result = verify_certificate(scenario, tampered, cert)
        assert not result.valid

    def test_tampered_provider_check_fails(self):
        scenario = _scenario("canonical.json")
        assignments = _canonical_optimal_assignments()
        cert = generate_certificate(scenario, assignments)
        tampered = [
            Assignment(deliverableId="d-design",  providerId="designco", minutes=180),
            Assignment(deliverableId="d-writing", providerId="copyco",   minutes=180),
            Assignment(deliverableId="d-coding",  providerId="designco", minutes=180),
        ]
        result = verify_certificate(scenario, tampered, cert)
        assert len(result.checks_failed) > 0

    def test_tampered_provider_digest_mismatch(self):
        scenario = _scenario("canonical.json")
        assignments = _canonical_optimal_assignments()
        cert = generate_certificate(scenario, assignments)
        # Even if we use a valid provider, changing provider changes allocation digest
        tampered = [
            Assignment(deliverableId="d-design",  providerId="devco",    minutes=480),  # devco self (valid)
            Assignment(deliverableId="d-writing", providerId="designco", minutes=480),
            Assignment(deliverableId="d-coding",  providerId="copyco",   minutes=480),
        ]
        result = verify_certificate(scenario, tampered, cert)
        assert not result.digest_matched


class TestVerifyCertificateTamperMinutes:
    """A5 required: tamper minutes → verify rejects."""

    def test_wrong_minutes_rejected(self):
        scenario = _scenario("canonical.json")
        assignments = _canonical_optimal_assignments()
        cert = generate_certificate(scenario, assignments)
        tampered = [
            Assignment(deliverableId="d-design",  providerId="designco", minutes=999),  # wrong
            Assignment(deliverableId="d-writing", providerId="copyco",   minutes=180),
            Assignment(deliverableId="d-coding",  providerId="devco",    minutes=180),
        ]
        result = verify_certificate(scenario, tampered, cert)
        assert not result.valid

    def test_wrong_minutes_digest_mismatch(self):
        scenario = _scenario("canonical.json")
        assignments = _canonical_optimal_assignments()
        cert = generate_certificate(scenario, assignments)
        tampered = [
            Assignment(deliverableId="d-design",  providerId="designco", minutes=999),
            Assignment(deliverableId="d-writing", providerId="copyco",   minutes=180),
            Assignment(deliverableId="d-coding",  providerId="devco",    minutes=180),
        ]
        result = verify_certificate(scenario, tampered, cert)
        assert not result.digest_matched


class TestVerifyCertificateTamperDigest:
    """A5 required: tamper digest in certificate → verify rejects."""

    def test_tampered_scenario_digest_rejected(self):
        scenario = _scenario("canonical.json")
        assignments = _canonical_optimal_assignments()
        cert = generate_certificate(scenario, assignments)

        # Build a certificate with tampered scenario digest
        tampered_cert = Certificate(
            schemaVersion="1",
            scenarioDigest="a" * 64,       # wrong
            allocationDigest=cert.allocationDigest,
            verification="valid",
            assumptions=cert.assumptions,
            checks=cert.checks,
        )
        result = verify_certificate(scenario, assignments, tampered_cert)
        assert not result.digest_matched
        assert not result.valid

    def test_tampered_allocation_digest_rejected(self):
        scenario = _scenario("canonical.json")
        assignments = _canonical_optimal_assignments()
        cert = generate_certificate(scenario, assignments)

        tampered_cert = Certificate(
            schemaVersion="1",
            scenarioDigest=cert.scenarioDigest,
            allocationDigest="b" * 64,     # wrong
            verification="valid",
            assumptions=cert.assumptions,
            checks=cert.checks,
        )
        result = verify_certificate(scenario, assignments, tampered_cert)
        assert not result.digest_matched
        assert not result.valid

    def test_both_digests_tampered_rejected(self):
        scenario = _scenario("canonical.json")
        assignments = _canonical_optimal_assignments()
        cert = generate_certificate(scenario, assignments)

        tampered_cert = Certificate(
            schemaVersion="1",
            scenarioDigest="c" * 64,
            allocationDigest="d" * 64,
            verification="valid",
            assumptions=cert.assumptions,
            checks=cert.checks,
        )
        result = verify_certificate(scenario, assignments, tampered_cert)
        assert not result.valid


class TestVerifyCertificateTamperScenario:
    """A5 required: modify scenario → scenario digest differs."""

    def test_modified_scenario_rejected(self):
        scenario_orig = _scenario("canonical.json")
        scenario_coord = _scenario("coordination.json")
        assignments = _canonical_optimal_assignments()

        # Generate cert against canonical scenario
        cert = generate_certificate(scenario_orig, assignments)

        # Verify against coordination scenario (different scenario) → digest mismatch
        result = verify_certificate(scenario_coord, assignments, cert)
        assert not result.digest_matched
        assert not result.valid

    def test_different_scenarios_produce_different_digests(self):
        d1 = _scenario_digest(_scenario("canonical.json"))
        d2 = _scenario_digest(_scenario("coordination.json"))
        assert d1 != d2

    def test_same_scenario_produces_same_digest(self):
        d1 = _scenario_digest(_scenario("canonical.json"))
        d2 = _scenario_digest(_scenario("canonical.json"))
        assert d1 == d2


class TestVerifyCertificateTamperDeliverable:
    """A5 required: tamper deliverable assignment → verify rejects."""

    def test_swapped_deliverable_rejected(self):
        scenario = _scenario("canonical.json")
        assignments = _canonical_optimal_assignments()
        cert = generate_certificate(scenario, assignments)

        # Swap d-design and d-writing providers
        tampered = [
            Assignment(deliverableId="d-design",  providerId="copyco",   minutes=180),  # copyco has no design
            Assignment(deliverableId="d-writing", providerId="designco", minutes=480),
            Assignment(deliverableId="d-coding",  providerId="devco",    minutes=180),
        ]
        result = verify_certificate(scenario, tampered, cert)
        assert not result.valid

    def test_extra_deliverable_rejected(self):
        scenario = _scenario("canonical.json")
        assignments = _canonical_optimal_assignments()
        cert = generate_certificate(scenario, assignments)

        # Add a duplicate assignment
        extra = assignments + [
            Assignment(deliverableId="d-design", providerId="designco", minutes=180),
        ]
        result = verify_certificate(scenario, extra, cert)
        assert not result.valid


class TestVerifyCertificateValid:
    """Positive cases."""

    def test_canonical_verifies_successfully(self):
        scenario = _scenario("canonical.json")
        assignments = _canonical_optimal_assignments()
        cert = generate_certificate(scenario, assignments)
        result = verify_certificate(scenario, assignments, cert)
        assert result.valid
        assert result.digest_matched
        assert result.checks_failed == []

    def test_coordination_verifies_successfully(self):
        scenario = _scenario("coordination.json")
        assignments = _coordination_optimal_assignments()
        cert = generate_certificate(scenario, assignments)
        result = verify_certificate(scenario, assignments, cert)
        assert result.valid

    def test_valid_result_has_recomputed_cert(self):
        scenario = _scenario("canonical.json")
        assignments = _canonical_optimal_assignments()
        cert = generate_certificate(scenario, assignments)
        result = verify_certificate(scenario, assignments, cert)
        assert result.recomputed_cert is not None
        assert isinstance(result.recomputed_cert, Certificate)

    def test_solver_certificate_verifies(self):
        """The certificate embedded in the solver's Result must verify."""
        scenario = _scenario("canonical.json")
        result_obj = optimize(scenario)
        assert result_obj.certificate is not None

        vr = verify_certificate(scenario, result_obj.assignments, result_obj.certificate)
        assert vr.valid

    def test_coordination_solver_certificate_verifies(self):
        scenario = _scenario("coordination.json")
        result_obj = optimize(scenario)
        assert result_obj.certificate is not None

        vr = verify_certificate(scenario, result_obj.assignments, result_obj.certificate)
        assert vr.valid

    def test_verification_result_bool(self):
        scenario = _scenario("canonical.json")
        assignments = _canonical_optimal_assignments()
        cert = generate_certificate(scenario, assignments)
        result = verify_certificate(scenario, assignments, cert)
        assert bool(result) is True

    def test_invalid_result_bool(self):
        scenario = _scenario("canonical.json")
        assignments = _canonical_optimal_assignments()
        cert = generate_certificate(scenario, assignments)
        tampered_cert = Certificate(
            schemaVersion="1",
            scenarioDigest="e" * 64,
            allocationDigest="f" * 64,
            verification="valid",
            assumptions=cert.assumptions,
            checks=cert.checks,
        )
        result = verify_certificate(scenario, assignments, tampered_cert)
        assert bool(result) is False


# ===========================================================================
# Certificate semantics
# ===========================================================================

class TestCertificateSemantics:

    def test_certificate_is_not_profitability_guarantee(self):
        """The assumptions must explicitly disclaim profit guarantees."""
        cert = generate_certificate(_scenario("canonical.json"), _canonical_optimal_assignments())
        text = " ".join(cert.assumptions)
        # Must mention the absence of monetary/profit claims
        assert any(
            word in text.lower()
            for word in ["profit", "monetary", "cash", "commercial"]
        ), "Certificate assumptions must disclaim monetary profit claims"

    def test_certificate_is_not_identity_credential(self):
        cert = generate_certificate(_scenario("canonical.json"), _canonical_optimal_assignments())
        text = " ".join(cert.assumptions)
        assert any(
            word in text.lower()
            for word in ["identity", "credential", "legal", "cryptographic"]
        ), "Certificate assumptions must disclaim identity/legal claims"

    def test_assumptions_mention_integer_minutes(self):
        cert = generate_certificate(_scenario("canonical.json"), _canonical_optimal_assignments())
        text = " ".join(cert.assumptions)
        assert "integer" in text.lower() or "minut" in text.lower()

    def test_assumptions_mention_quality_equivalence(self):
        cert = generate_certificate(_scenario("canonical.json"), _canonical_optimal_assignments())
        text = " ".join(cert.assumptions)
        assert "quality" in text.lower()

    def test_digest_changes_when_scenario_changes(self):
        """Any change to the scenario must change the scenario digest."""
        s1 = _scenario("canonical.json")
        # Modify one capacity
        data = json.loads(s1.model_dump_json())
        data["businesses"][0]["capacityMinutes"] = 999
        s2 = Scenario.model_validate(data)
        assert _scenario_digest(s1) != _scenario_digest(s2)

    def test_digest_changes_when_assignments_change(self):
        """Any change to the assignment list must change the allocation digest."""
        asgns1 = _canonical_optimal_assignments()
        asgns2 = [
            Assignment(deliverableId="d-design",  providerId="devco",    minutes=480),  # self, different
            Assignment(deliverableId="d-writing", providerId="copyco",   minutes=180),
            Assignment(deliverableId="d-coding",  providerId="devco",    minutes=180),
        ]
        assert _allocation_digest(asgns1) != _allocation_digest(asgns2)

    def test_display_rounding_does_not_alter_digest(self):
        """
        Converting 480 minutes to 8 hours for display must not change the source.
        The digest is computed on integer minutes.
        """
        scenario = _scenario("canonical.json")
        assignments = _canonical_optimal_assignments()
        cert = generate_certificate(scenario, assignments)

        # Digest must be based on integer minutes, not any hour conversion
        # Confirm by checking allocation digest uses raw minutes
        raw = [{"deliverableId": a.deliverableId, "providerId": a.providerId, "minutes": a.minutes}
               for a in assignments]
        from optimizer.certificate import _canonical_json, _sha256_hex
        expected_digest = _sha256_hex(_canonical_json(raw))
        assert cert.allocationDigest == expected_digest


# ===========================================================================
# _run_checks — unit tests for the internal check runner
# ===========================================================================

class TestRunChecksUnit:

    def test_all_checks_pass_on_canonical(self):
        checks = _run_checks(_scenario("canonical.json"), _canonical_optimal_assignments())
        failed = [(c.name, c.reason) for c in checks if not c.passed]
        assert failed == [], f"Unexpected failures: {failed}"

    def test_missing_deliverable_fails_coverage(self):
        checks = _run_checks(_scenario("canonical.json"), [
            Assignment(deliverableId="d-design", providerId="designco", minutes=180),
        ])
        coverage = next(c for c in checks if c.name == "coverage_complete")
        assert not coverage.passed

    def test_unknown_provider_fails(self):
        checks = _run_checks(_scenario("canonical.json"), [
            Assignment(deliverableId="d-design",  providerId="ghost",    minutes=180),
            Assignment(deliverableId="d-writing", providerId="copyco",   minutes=180),
            Assignment(deliverableId="d-coding",  providerId="devco",    minutes=180),
        ])
        ids_check = next(c for c in checks if c.name == "all_ids_known")
        assert not ids_check.passed

    def test_wrong_minutes_fails_duration(self):
        checks = _run_checks(_scenario("canonical.json"), [
            Assignment(deliverableId="d-design",  providerId="designco", minutes=999),
            Assignment(deliverableId="d-writing", providerId="copyco",   minutes=180),
            Assignment(deliverableId="d-coding",  providerId="devco",    minutes=180),
        ])
        dur_check = next(c for c in checks if c.name == "duration_matches_estimate")
        assert not dur_check.passed
