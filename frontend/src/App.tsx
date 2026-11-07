import { useEffect, useMemo, useReducer, useState } from "react";
import type { HealthResponse } from "../../contracts/models";
import { ApiClient, describeError } from "./lib/api";
import BusinessForm from "./components/BusinessForm";
import ExtractionReview from "./components/ExtractionReview";
import { readConfig } from "./config";
import { initialState, scenarioReducer, validateForm } from "./state/scenario";

type HealthState =
  | { kind: "checking" }
  | { kind: "ok"; health: HealthResponse }
  | { kind: "unavailable"; message: string };

const NOT_BUILT = [
  "Optimization, exchange graph and certificate",
  "Natural-language what-if simulation",
];

export default function App() {
  const config = useMemo(() => readConfig(), []);
  const client = useMemo(
    () => new ApiClient({ apiBaseUrl: config.apiBaseUrl, dataMode: config.dataMode }),
    [config],
  );
  const [state, setState] = useState<HealthState>({ kind: "checking" });
  const isFixture = config.dataMode === "fixture";
  const [scenario, dispatch] = useReducer(scenarioReducer, undefined, initialState);
  const validation = useMemo(() => validateForm(scenario.form), [scenario.form]);

  useEffect(() => {
    // The cleanup aborts the request so a late response can never overwrite newer state.
    const controller = new AbortController();
    setState({ kind: "checking" });
    client
      .health({ signal: controller.signal })
      .then((health) => setState({ kind: "ok", health }))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        // Live failures are shown as failures. There is no fallback to fixture data.
        setState({ kind: "unavailable", message: describeError(error) });
      });
    return () => controller.abort();
  }, [client]);

  const modeMismatch = state.kind === "ok" && state.health.mode !== config.dataMode;

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <a href="#workspace" className="skip-link">
        Skip to workspace
      </a>

      <div
        role="status"
        data-testid="mode-banner"
        className={
          isFixture
            ? "bg-amber-300 px-4 py-2 text-center text-sm font-semibold text-amber-950"
            : "bg-emerald-800 px-4 py-2 text-center text-sm font-semibold text-white"
        }
      >
        {isFixture
          ? "FIXTURE MODE: SIMULATED data only. Nothing here comes from AWS."
          : "LIVE MODE: results come from the backend. Failures are shown, never replaced by sample data."}
      </div>

      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl flex-wrap items-baseline justify-between gap-2 px-4 py-4">
          <h1 className="text-2xl font-semibold tracking-tight">TradeMind</h1>
          <p className="text-sm text-slate-600">
            Models labor-time exchanges under supplied estimates. Not a profit or quality guarantee.
          </p>
        </div>
      </header>

      <main id="workspace" className="mx-auto max-w-6xl px-4 py-6">
        {config.configError && (
          <p role="alert" className="mb-4 rounded border border-red-300 bg-red-50 p-3 text-red-900">
            {config.configError}
          </p>
        )}
        {modeMismatch && state.kind === "ok" && (
          <p role="alert" className="mb-4 rounded border border-red-300 bg-red-50 p-3 text-red-900">
            Mode mismatch: this app is in {config.dataMode} mode but the backend reports {state.health.mode} mode.
            Fix VITE_DATA_MODE or APP_MODE before trusting any result.
          </p>
        )}

        <div className="grid gap-6 lg:grid-cols-2">
          <section aria-labelledby="inputs-heading" className="rounded border border-slate-200 bg-white p-4">
            <h2 id="inputs-heading" className="text-lg font-medium">
              Businesses and scenario
            </h2>
            <p className="mt-2 text-sm text-slate-700">
              Durations are shown in hours but stored as whole minutes. Sample data is fictional.
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
              <button
                type="button"
                className="rounded border border-slate-500 bg-white px-3 py-1.5 text-sm font-medium"
                onClick={() => {
                  dispatch({ type: "load_canonical" });
                }}
              >
                Load fictional sample
              </button>
              <button
                type="button"
                className="rounded border border-slate-500 bg-white px-3 py-1.5 text-sm font-medium"
                onClick={() => {
                  dispatch({ type: "reset" });
                }}
              >
                Reset to blank
              </button>
            </div>
            <div className="mt-4 space-y-4">
              {scenario.form.businesses.map((business, index) => (
                <BusinessForm
                  key={index}
                  index={index}
                  business={business}
                  errors={validation.errors}
                  onChange={(i, patch) => {
                    dispatch({ type: "edit_business", index: i, patch });
                  }}
                />
              ))}
            </div>
            <div className="mt-4" aria-live="polite" data-testid="validity">
              {validation.scenario ? (
                <p className="text-emerald-900">Input is valid.</p>
              ) : (
                <p className="text-red-800">
                  {validation.errors.length} problem{validation.errors.length === 1 ? "" : "s"} to fix before continuing.
                </p>
              )}
            </div>
            <ExtractionReview
              client={client}
              form={scenario.form}
              version={scenario.version}
              validation={validation}
              confirmed={scenario.confirmed}
              onApplyDraft={(form) => dispatch({ type: "apply_draft", form })}
              onConfirmed={(forVersion, confirmed) => dispatch({ type: "set_confirmed", forVersion, scenario: confirmed })}
            />
            <h3 className="mt-4 text-sm font-semibold uppercase tracking-wide text-slate-600">Not built yet</h3>
            <ul className="mt-2 list-disc pl-5 text-slate-700">
              {NOT_BUILT.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </section>

          <section aria-labelledby="results-heading" className="rounded border border-slate-200 bg-white p-4">
            <h2 id="results-heading" className="text-lg font-medium">
              Results and backend
            </h2>
            <p className="mt-1 break-all text-sm text-slate-600" data-testid="api-base-url">
              {config.apiBaseUrl}
            </p>
            <p className="mt-3 text-sm text-slate-700" data-testid="result-slot">
              {scenario.result
                ? "A result for the current inputs is available."
                : "No result. Results and certificates are cleared whenever the inputs change."}
            </p>
            <div className="mt-3" data-testid="health-state" aria-live="polite">
              {state.kind === "checking" && <p>Checking /health…</p>}
              {state.kind === "ok" && (
                <p className="text-emerald-900">
                  Backend OK. Schema v{state.health.schemaVersion}, mode <strong>{state.health.mode}</strong>, commit{" "}
                  <code>{state.health.commit}</code>.
                </p>
              )}
              {state.kind === "unavailable" && (
                <p role="alert" className="text-red-800">
                  Backend unavailable. {state.message}
                </p>
              )}
            </div>
          </section>
        </div>
      </main>
    </div>
  );
}
