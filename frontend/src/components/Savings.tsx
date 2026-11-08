import { useEffect, useId, useState } from "react";
import type { BusinessMetrics, Result, Scenario } from "../../../contracts/models";
import { ApiClient } from "../lib/api";
import { AsyncError, Pending, useLatestRequest } from "./AsyncState";
import ScrollRegion from "./ScrollRegion";
import { formatDuration } from "../state/scenario";
import { describeMinutes } from "./NetworkGraph";

/** "24 h", "0.25 h" or "10 min": exact, never rounded. */
export function hoursText(minutes: number): string {
  const text = formatDuration(minutes);
  return text.endsWith("m") ? `${minutes} min` : `${text} h`;
}

/**
 * Percentage of `whole` by integer arithmetic, truncated (never rounded up) to one decimal.
 * Returns null unless `whole` is positive and `part` is non-negative, so a zero baseline
 * never produces NaN, Infinity or an invented figure.
 */
export function percent(part: number, whole: number): string | null {
  if (!Number.isInteger(part) || !Number.isInteger(whole) || whole <= 0 || part < 0) return null;
  const tenths = Math.floor((part * 1000) / whole);
  const rest = tenths % 10;
  return `${Math.trunc(tenths / 10)}${rest === 0 ? "" : `.${rest}`}%`;
}

/**
 * Checks the server's numbers against each other. The browser does not compute savings; this
 * only detects a result whose own figures disagree, so that it is flagged instead of shown as fine.
 */
export function findInconsistencies(result: Result): string[] {
  const { businessMetrics: metrics, totals } = result;
  if (!metrics || !totals) return [];
  const problems: string[] = [];
  if (totals.grossSavingsMinutes !== totals.baselineMinutes - totals.productionMinutes) {
    problems.push("Gross savings is not baseline minus production.");
  }
  if (totals.netSavingsMinutes !== totals.baselineMinutes - totals.effortMinutes) {
    problems.push("Net savings is not baseline minus effort.");
  }
  if (totals.effortMinutes !== totals.productionMinutes + totals.coordinationMinutes) {
    problems.push("Total effort is not production plus coordination.");
  }
  for (const m of metrics) {
    if (m.effortMinutes !== m.productionMinutes + m.coordinationMinutes) {
      problems.push(`${m.businessId}: effort is not production plus coordination.`);
    }
    if (m.netSavingsMinutes !== m.baselineMinutes - m.effortMinutes) {
      problems.push(`${m.businessId}: net savings is not baseline minus effort.`);
    }
    if (m.netSavingsMinutes < 0) problems.push(`${m.businessId}: net savings is negative.`);
  }
  const sum = (pick: (m: BusinessMetrics) => number) => metrics.reduce((acc, m) => acc + pick(m), 0);
  const pairs: [string, number, number][] = [
    ["baseline", sum((m) => m.baselineMinutes), totals.baselineMinutes],
    ["production", sum((m) => m.productionMinutes), totals.productionMinutes],
    ["coordination", sum((m) => m.coordinationMinutes), totals.coordinationMinutes],
    ["effort", sum((m) => m.effortMinutes), totals.effortMinutes],
    ["net savings", sum((m) => m.netSavingsMinutes), totals.netSavingsMinutes],
  ];
  for (const [label, parts, total] of pairs) {
    if (parts !== total) problems.push(`Per-business ${label} does not add up to the total.`);
  }
  return problems;
}

interface Props {
  scenario: Scenario;
  result: Result;
  client: ApiClient;
}

export default function Savings({ scenario, result, client }: Props) {
  const nameOf = (id: string) => scenario.businesses.find((b) => b.id === id)?.name ?? id;
  const { businessMetrics: metrics, totals } = result;

  if (!metrics || !totals) {
    return (
      <p className="rounded bg-slate-100 p-2 text-sm" data-testid="no-savings">
        No savings figures: this result has no valid allocation, so nothing was computed.
      </p>
    );
  }

  const problems = findInconsistencies(result);
  const grossPct = percent(totals.grossSavingsMinutes, totals.baselineMinutes);
  const netPct = percent(totals.netSavingsMinutes, totals.baselineMinutes);

  return (
    <div className="space-y-3" data-testid="savings">
      {problems.length > 0 && (
        <div role="alert" className="rounded border border-red-300 bg-red-50 p-2 text-sm text-red-900">
          <p className="font-medium">The server's figures disagree with each other. Do not rely on them.</p>
          <ul className="list-disc pl-5">
            {problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        </div>
      )}

      <p className="text-base font-semibold" data-testid="headline">
        Gross: {hoursText(totals.baselineMinutes)} → {hoursText(totals.productionMinutes)} of production. Including
        coordination: {hoursText(totals.baselineMinutes)} → {hoursText(totals.effortMinutes)} of effort.
      </p>

      <ScrollRegion label="Effort per business">
        <table className="w-full text-left text-sm" data-testid="metrics">
          <caption className="mb-1 text-left text-sm font-semibold">Effort per business</caption>
          <thead>
            <tr className="border-b border-slate-300">
              <th scope="col" className="py-1 pr-2">Business</th>
              <th scope="col" className="pr-2">Baseline</th>
              <th scope="col" className="pr-2">Production</th>
              <th scope="col" className="pr-2">Coordination</th>
              <th scope="col" className="pr-2">Effort</th>
              <th scope="col" className="pr-2">Net savings</th>
              <th scope="col">Net savings, % of baseline</th>
            </tr>
          </thead>
          <tbody>
            {metrics.map((m) => (
              <tr key={m.businessId} className="border-b border-slate-200" data-testid={`row-${m.businessId}`}>
                <th scope="row" className="py-1 pr-2 font-medium">{nameOf(m.businessId)}</th>
                <td className="pr-2">{describeMinutes(m.baselineMinutes)}</td>
                <td className="pr-2">{describeMinutes(m.productionMinutes)}</td>
                <td className="pr-2">{describeMinutes(m.coordinationMinutes)}</td>
                <td className="pr-2">{describeMinutes(m.effortMinutes)}</td>
                <td className="pr-2">{describeMinutes(m.netSavingsMinutes)}</td>
                <td>{percent(m.netSavingsMinutes, m.baselineMinutes) ?? "n/a (baseline is 0)"}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className="font-semibold" data-testid="row-total">
              <th scope="row" className="py-1 pr-2">Total</th>
              <td className="pr-2">{describeMinutes(totals.baselineMinutes)}</td>
              <td className="pr-2">{describeMinutes(totals.productionMinutes)}</td>
              <td className="pr-2">{describeMinutes(totals.coordinationMinutes)}</td>
              <td className="pr-2">{describeMinutes(totals.effortMinutes)}</td>
              <td className="pr-2">{describeMinutes(totals.netSavingsMinutes)}</td>
              <td>{netPct ?? "n/a (baseline is 0)"}</td>
            </tr>
          </tfoot>
        </table>
      </ScrollRegion>

      <p className="text-sm" data-testid="savings-summary">
        Gross savings (baseline minus production): {describeMinutes(totals.grossSavingsMinutes)}
        {grossPct ? `, ${grossPct} of baseline` : ""}. Net savings (after coordination):{" "}
        {describeMinutes(totals.netSavingsMinutes)}
        {netPct ? `, ${netPct} of baseline` : ""}.
        {totals.baselineMinutes <= 0 && " No percentage is shown because the total baseline is 0."}
      </p>

      <p className="text-xs text-slate-600" data-testid="savings-provenance">
        Minutes come from the {client.dataMode === "fixture" ? "SIMULATED (fixture mode) backend" : "live backend"}.
        Percentages are derived in this browser from those minutes, truncated to one decimal and never rounded up.
        These are modeled labor-time figures under the supplied estimates, not profit, revenue or a quality guarantee.
      </p>

      <ExplainSection scenario={scenario} result={result} client={client} />
    </div>
  );
}

type ExplainState =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "ready"; text: string; mode: string }
  | { kind: "error"; error: unknown };

/**
 * Optional prose from POST /explain. It is shown below the numbers, labelled as generated text,
 * fails on its own and never changes anything above it.
 */
function ExplainSection({ scenario, result, client }: Props) {
  const headingId = useId();
  const [state, setState] = useState<ExplainState>({ kind: "idle" });
  const { start, invalidate } = useLatestRequest();

  // A different result, scenario or client (mode) makes any earlier explanation stale.
  useEffect(() => {
    invalidate();
    setState({ kind: "idle" });
  }, [result, scenario, client, invalidate]);

  async function run() {
    const request = start();
    if (!request) return; // already running: no double submit
    setState({ kind: "pending" });
    try {
      const response = await client.explain({ scenario, result }, { signal: request.signal });
      if (!request.isCurrent()) return;
      setState({ kind: "ready", text: response.text, mode: response.mode });
    } catch (error) {
      if (!request.isCurrent() || (error instanceof DOMException && error.name === "AbortError")) return;
      setState({ kind: "error", error });
    } finally {
      request.done();
    }
  }

  return (
    <div role="group" aria-labelledby={headingId} className="rounded border border-slate-200 p-2" data-testid="explain">
      <h4 id={headingId} className="text-sm font-semibold">
        Plain-language explanation (optional)
      </h4>
      <button
        type="button"
        onClick={run}
        aria-disabled={state.kind === "pending"}
        className="mt-1 rounded border border-slate-500 bg-white px-3 py-1 text-sm font-medium aria-disabled:cursor-not-allowed aria-disabled:opacity-60"
      >
        {state.kind === "pending" ? "Explaining…" : "Explain these results"}
      </button>
      {state.kind === "pending" && <Pending>Writing the explanation…</Pending>}
      {state.kind === "error" && (
        <AsyncError
          error={state.error}
          prefix="Explanation unavailable."
          suffix="The numbers above are unaffected."
          onRetry={run}
          testId="explain-error"
        />
      )}
      {state.kind === "ready" && (
        <div className="mt-1 text-sm" data-testid="explain-text">
          <p className="whitespace-pre-wrap">{state.text}</p>
          <p className="mt-1 text-xs text-slate-600">
            Generated text ({state.mode === "fixture" ? "SIMULATED, fixture mode" : "live"}). If it differs from the
            figures above, the figures are authoritative.
          </p>
        </div>
      )}
    </div>
  );
}
