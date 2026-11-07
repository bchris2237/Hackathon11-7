import { useEffect, useState } from "react";
import type { Result, Scenario } from "../../../contracts/models";
import { ApiClient } from "../lib/api";
import { AsyncError, EmptyState, Pending, useLatestRequest } from "./AsyncState";
import Certificate from "./Certificate";
import NetworkGraph from "./NetworkGraph";
import Savings from "./Savings";

interface Props {
  client: ApiClient;
  /** Backend-confirmed scenario for the current form version, or null. */
  confirmed: Scenario | null;
  /** Bumped by the parent on every edit; in-flight requests for older versions are dropped. */
  version: number;
  result: Result | null;
  onResult: (forVersion: number, result: Result) => void;
}

type Status = { kind: "idle" } | { kind: "pending" } | { kind: "error"; error: unknown };

export const STATUS_TEXT: Record<Result["status"], string> = {
  collaboration: "Beneficial exchange found",
  no_exchange: "Feasible, but no beneficial exchange",
  infeasible: "Infeasible: no valid allocation",
};

export default function OptimizePanel({ client, confirmed, version, result, onResult }: Props) {
  const [status, setStatus] = useState<Status>({ kind: "idle" });
  const { start, invalidate } = useLatestRequest();

  // Any edit (and any switch of the client, i.e. of mode) invalidates in-flight work: abort it
  // and mark its late reply as stale.
  useEffect(() => {
    invalidate();
    setStatus({ kind: "idle" });
  }, [version, client, invalidate]);

  async function run() {
    if (!confirmed) return;
    const request = start();
    if (!request) return; // already running: no double submit
    const forVersion = version;
    setStatus({ kind: "pending" });
    try {
      const response = await client.optimize({ scenario: confirmed }, { signal: request.signal });
      if (!request.isCurrent()) return;
      setStatus({ kind: "idle" });
      onResult(forVersion, response.result);
    } catch (error) {
      if (!request.isCurrent() || (error instanceof DOMException && error.name === "AbortError")) return;
      // No fallback result is shown; any previous result was already cleared by the edit.
      setStatus({ kind: "error", error });
    } finally {
      request.done();
    }
  }

  return (
    <div className="mt-4 space-y-3" data-testid="optimize-panel">
      <button
        type="button"
        onClick={run}
        disabled={!confirmed || status.kind === "pending"}
        className="rounded bg-blue-800 px-4 py-2 font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-400"
      >
        {status.kind === "pending" ? "Optimizing…" : "Optimize"}
      </button>
      {!confirmed && <EmptyState>Confirm the scenario first. Optimization runs only on a confirmed scenario.</EmptyState>}
      {status.kind === "pending" && <Pending>Calculating the best allocation…</Pending>}
      {status.kind === "error" && (
        <AsyncError error={status.error} prefix="Optimization failed." onRetry={run} testId="optimize-error" />
      )}

      {result && confirmed && (
        <section aria-labelledby="opt-heading" data-testid="optimize-result" className="space-y-3">
          <h3 id="opt-heading" className="text-base font-semibold">
            Result: <span data-testid="result-status">{STATUS_TEXT[result.status]}</span>
          </h3>

          {result.reasons.length > 0 && (
            <ul className="list-disc rounded bg-slate-100 p-2 pl-6 text-sm" data-testid="reasons">
              {result.reasons.map((r) => (
                <li key={`${r.code}-${r.message}`}>
                  <code>{r.code}</code>: {r.message}
                </li>
              ))}
            </ul>
          )}

          {result.status !== "infeasible" && result.totals && result.businessMetrics && (
            <NetworkGraph scenario={confirmed} result={result} />
          )}
          <Savings scenario={confirmed} result={result} client={client} />
          <Certificate result={result} mode={client.dataMode} />
        </section>
      )}
    </div>
  );
}
