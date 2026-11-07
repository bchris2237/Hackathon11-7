import { useEffect, useState } from "react";
import type { HealthResponse } from "../../contracts/models";
import { ApiUnavailableError, fetchHealth } from "./api";
import { readConfig } from "./config";

type HealthState =
  | { kind: "checking" }
  | { kind: "ok"; health: HealthResponse }
  | { kind: "unavailable"; message: string };

const NOT_BUILT = [
  "Business description input and extraction",
  "Human confirmation of the draft scenario",
  "Optimization, exchange graph and certificate",
  "Natural-language what-if simulation",
];

export default function App() {
  const config = readConfig();
  const [state, setState] = useState<HealthState>({ kind: "checking" });

  useEffect(() => {
    // The cleanup aborts the request so a late response can never overwrite newer state.
    const controller = new AbortController();
    setState({ kind: "checking" });
    fetchHealth(config.apiBaseUrl, controller.signal)
      .then((health) => setState({ kind: "ok", health }))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        const message = error instanceof ApiUnavailableError ? error.message : "Unexpected error checking the backend.";
        setState({ kind: "unavailable", message });
      });
    return () => controller.abort();
  }, [config.apiBaseUrl]);

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <div
        role="status"
        data-testid="mode-banner"
        className={
          config.dataMode === "fixture"
            ? "bg-amber-300 px-4 py-2 text-center text-sm font-semibold text-amber-950"
            : "bg-emerald-700 px-4 py-2 text-center text-sm font-semibold text-white"
        }
      >
        {config.dataMode === "fixture"
          ? "FIXTURE MODE: simulated data only. Nothing here comes from AWS."
          : "LIVE MODE: the backend is expected to use real services."}
      </div>

      <main className="mx-auto max-w-3xl px-4 py-10">
        <h1 className="text-3xl font-semibold">TradeMind</h1>
        <p className="mt-2 text-slate-600">
          Phase 0 shell. No product features are built yet; this page only checks that the backend is reachable.
        </p>

        {config.configError && (
          <p role="alert" className="mt-4 rounded border border-red-300 bg-red-50 p-3 text-red-800">
            {config.configError}
          </p>
        )}

        <section aria-labelledby="backend-heading" className="mt-8 rounded border border-slate-200 bg-white p-4">
          <h2 id="backend-heading" className="text-lg font-medium">
            Backend
          </h2>
          <p className="mt-1 text-sm text-slate-500" data-testid="api-base-url">
            {config.apiBaseUrl}
          </p>
          <div className="mt-3" data-testid="health-state" aria-live="polite">
            {state.kind === "checking" && <p>Checking /health…</p>}
            {state.kind === "ok" && (
              <p className="text-emerald-800">
                Backend OK. Schema v{state.health.schemaVersion}, mode <strong>{state.health.mode}</strong>, commit{" "}
                <code>{state.health.commit}</code>.
              </p>
            )}
            {state.kind === "unavailable" && (
              <p role="alert" className="text-red-700">
                Backend unavailable. {state.message}
              </p>
            )}
          </div>
        </section>

        <section aria-labelledby="todo-heading" className="mt-6 rounded border border-slate-200 bg-white p-4">
          <h2 id="todo-heading" className="text-lg font-medium">
            Not built yet
          </h2>
          <ul className="mt-2 list-disc pl-5 text-slate-700">
            {NOT_BUILT.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </section>
      </main>
    </div>
  );
}
