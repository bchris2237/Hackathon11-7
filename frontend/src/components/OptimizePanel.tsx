import { useEffect, useRef, useState } from "react";
import type { Result, Scenario } from "../../../contracts/models";
import { ApiClient, describeError } from "../lib/api";
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

type Status = { kind: "idle" } | { kind: "pending" } | { kind: "error"; message: string };

export const STATUS_TEXT: Record<Result["status"], string> = {
  collaboration: "Beneficial exchange found",
  no_exchange: "Feasible, but no beneficial exchange",
  infeasible: "Infeasible: no valid allocation",
};

export default function OptimizePanel({ client, confirmed, version, result, onResult }: Props) {
  const [status, setStatus] = useState<Status>({ kind: "idle" });
  const seq = useRef(0);
  const controller = useRef<AbortController | null>(null);

  // Any edit invalidates in-flight work: abort and bump the id so late replies are ignored.
  useEffect(() => {
    seq.current += 1;
    controller.current?.abort();
    controller.current = null;
    setStatus({ kind: "idle" });
  }, [version]);
  useEffect(() => () => controller.current?.abort(), []);

  async function run() {
    if (!confirmed) return;
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    seq.current += 1;
    const id = seq.current;
    const forVersion = version;
    setStatus({ kind: "pending" });
    try {
      const response = await client.optimize({ scenario: confirmed }, { signal: next.signal });
      if (id !== seq.current) return;
      setStatus({ kind: "idle" });
      onResult(forVersion, response.result);
    } catch (error) {
      if (id !== seq.current || (error instanceof DOMException && error.name === "AbortError")) return;
      // No fallback result is shown; any previous result was already cleared by the edit.
      setStatus({ kind: "error", message: describeError(error) });
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
      {!confirmed && <p className="text-sm text-slate-700">Confirm the scenario first. Optimization runs only on a confirmed scenario.</p>}
      {status.kind === "error" && (
        <p role="alert" className="text-sm text-red-800">
          Optimization failed. {status.message}
        </p>
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
