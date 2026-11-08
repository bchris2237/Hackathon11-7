import { useId } from "react";
import type { Certificate as CertificateData, DataMode, Result } from "../../../contracts/models";

const DIGEST = /^[0-9a-f]{64}$/;

export type CertificateCheckResult = { ok: true; certificate: CertificateData } | { ok: false; problems: string[] };

/**
 * Runtime check of a certificate against the v1 contract. Anything that does not match is
 * reported as invalid and is never shown as "verified". This only checks the shape the server
 * sent; the browser does not recompute digests and the backend stays authoritative.
 */
export function checkCertificate(value: unknown): CertificateCheckResult {
  const problems: string[] = [];
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return { ok: false, problems: ["The certificate is not an object."] };
  }
  const c = value as Record<string, unknown>;
  const allowed = new Set(["schemaVersion", "scenarioDigest", "allocationDigest", "verification", "assumptions", "checks"]);
  for (const key of Object.keys(c)) if (!allowed.has(key)) problems.push(`Unknown field "${key}".`);
  if (c.schemaVersion !== "1") problems.push("Unsupported schemaVersion.");
  if (typeof c.scenarioDigest !== "string" || !DIGEST.test(c.scenarioDigest)) problems.push("scenarioDigest is not a SHA-256 hex digest.");
  if (typeof c.allocationDigest !== "string" || !DIGEST.test(c.allocationDigest)) problems.push("allocationDigest is not a SHA-256 hex digest.");
  if (c.verification !== "valid") problems.push('verification is not "valid".');
  if (!Array.isArray(c.assumptions) || !c.assumptions.every((a) => typeof a === "string" && a.length > 0)) {
    problems.push("assumptions is not a list of non-empty strings.");
  }
  if (!Array.isArray(c.checks) || c.checks.length === 0) {
    problems.push("The certificate lists no checks.");
  } else {
    c.checks.forEach((check, i) => {
      const k = check as Record<string, unknown> | null;
      if (typeof k?.name !== "string" || k.name.length === 0) problems.push(`Check ${i + 1} has no name.`);
      if (k?.passed !== true) problems.push(`Check ${i + 1}${typeof k?.name === "string" ? ` ("${k.name}")` : ""} did not pass.`);
    });
  }
  return problems.length === 0 ? { ok: true, certificate: value as CertificateData } : { ok: false, problems };
}

interface Props {
  result: Result;
  /** Mode the app is configured for; ApiClient has already rejected any response in another mode. */
  mode: DataMode;
}

export default function Certificate({ result, mode }: Props) {
  const headingId = useId();
  if (result.status === "infeasible") {
    return (
      <div role="group" aria-labelledby={headingId} className="rounded border border-slate-200 p-3" data-testid="certificate">
        <h4 id={headingId} className="text-sm font-semibold">Verification certificate</h4>
        <p className="mt-1 text-sm" data-testid="certificate-none">
          None. An infeasible result has no allocation to verify, so no certificate is issued.
        </p>
      </div>
    );
  }

  const checked = result.certificate === null ? null : checkCertificate(result.certificate);

  return (
    <div role="group" aria-labelledby={headingId} className="space-y-2 rounded border border-slate-200 p-3" data-testid="certificate">
      <h4 id={headingId} className="text-sm font-semibold">Verification certificate</h4>

      <p className="text-sm" data-testid="certificate-provenance">
        Source:{" "}
        {mode === "fixture" ? (
          <strong className="text-amber-900">SIMULATED (fixture mode). Not a live verification</strong>
        ) : (
          <strong>live backend</strong>
        )}
        . The server recomputed the arithmetic and constraints; this browser did not.
      </p>

      {checked === null && (
        <p role="alert" className="rounded border border-red-300 bg-red-50 p-2 text-sm text-red-900" data-testid="certificate-missing">
          The server returned no certificate for this result. These figures are NOT verified.
        </p>
      )}

      {checked && !checked.ok && (
        <div role="alert" className="rounded border border-red-300 bg-red-50 p-2 text-sm text-red-900" data-testid="certificate-invalid">
          <p className="font-medium">The certificate is invalid, so these figures are NOT shown as verified.</p>
          <ul className="list-disc pl-5">
            {checked.problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        </div>
      )}

      {checked && checked.ok && (
        <>
          <p className="text-sm font-medium text-emerald-900" data-testid="certificate-status">
            Server recomputation: {checked.certificate.checks.length} checks passed.
          </p>
          <ul className="list-disc pl-5 text-sm" data-testid="certificate-checks">
            {checked.certificate.checks.map((check, i) => (
              <li key={i}>{check.name}: passed</li>
            ))}
          </ul>
          <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-sm">
            <dt className="font-medium">Scenario digest</dt>
            <dd className="break-all font-mono text-xs" data-testid="scenario-digest">{checked.certificate.scenarioDigest}</dd>
            <dt className="font-medium">Allocation digest</dt>
            <dd className="break-all font-mono text-xs" data-testid="allocation-digest">{checked.certificate.allocationDigest}</dd>
          </dl>
          <div>
            <h5 className="text-sm font-medium">Assumptions</h5>
            {checked.certificate.assumptions.length === 0 ? (
              <p className="text-sm text-slate-700">The server listed no assumptions.</p>
            ) : (
              <ul className="list-disc pl-5 text-sm" data-testid="certificate-assumptions">
                {checked.certificate.assumptions.map((a, i) => (
                  <li key={i}>{a}</li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}

      <p className="text-xs text-slate-600" data-testid="certificate-limits">
        This certificate shows that the arithmetic and constraints were recomputed under the supplied estimates. It is not
        a signature, not tamper-proof, not an identity credential, and not a guarantee of profit, payment or service
        quality. The digests only help detect whether two runs used different inputs.
      </p>
    </div>
  );
}
