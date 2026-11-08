import { useEffect, useMemo, useReducer, useState } from "react";
import type { DataMode, HealthResponse } from "../../contracts/models";
import { ApiClient } from "./lib/api";
import { AsyncError, EmptyState, Pending } from "./components/AsyncState";
import BusinessForm from "./components/BusinessForm";
import ExtractionReview from "./components/ExtractionReview";
import OptimizePanel from "./components/OptimizePanel";
import Simulation from "./components/Simulation";
import { readConfig } from "./config";
import { initialState, scenarioReducer, validateForm } from "./state/scenario";

type HealthState =
  | { kind: "checking" }
  | { kind: "ok"; health: HealthResponse }
  | { kind: "unavailable"; error: unknown };

const NOT_BUILT = [
  "Saved profiles (optional enhancement)",
  "Certificate download (optional enhancement)",
];

export default function App() {
  const config = useMemo(() => readConfig(), []);
  // The mode starts from VITE_DATA_MODE and only changes when the user switches it explicitly.
  const [dataMode, setDataMode] = useState<DataMode>(config.dataMode);
  const client = useMemo(
    () => new ApiClient({ apiBaseUrl: config.apiBaseUrl, dataMode }),
    [config.apiBaseUrl, dataMode],
  );
  const [state, setState] = useState<HealthState>({ kind: "checking" });
  const [healthAttempt, setHealthAttempt] = useState(0);
  const isFixture = dataMode === "fixture";
  const [scenario, dispatch] = useReducer(scenarioReducer, undefined, initialState);
  const validation = useMemo(() => validateForm(scenario.form), [scenario.form]);
  const [switchNote, setSwitchNote] = useState<{ mode: DataMode; version: number } | null>(null);

  // Switching mode is deliberate and only offered when the backend reports the other mode, so
  // it can never be clicked by accident into a mismatch. It keeps what the user typed but drops
  // everything produced under the other mode (confirmation, result, what-if) and cancels requests in flight.
  function switchMode() {
    const next: DataMode = dataMode === "fixture" ? "live" : "fixture";
    dispatch({ type: "reset_derived" });
    setDataMode(next);
    setSwitchNote({ mode: next, version: scenario.version + 1 });
  }
  // The note goes away at the next edit.
  useEffect(() => {
    if (switchNote && scenario.version > switchNote.version) setSwitchNote(null);
  }, [scenario.version, switchNote]);

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
        setState({ kind: "unavailable", error });
      });
    return () => controller.abort();
  }, [client, healthAttempt]);

  const modeMismatch = state.kind === "ok" && state.health.mode !== dataMode;

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

      <main id="workspace" tabIndex={-1} className="mx-auto max-w-6xl px-4 py-6 focus:outline-none">
        {config.configError && (
          <p role="alert" className="mb-4 rounded border border-red-300 bg-red-50 p-3 text-red-900">
            {config.configError}
          </p>
        )}
        {switchNote && (
          <p role="status" data-testid="mode-switch-note" className="mb-4 rounded border border-slate-300 bg-white p-3 text-sm">
            Switched to {switchNote.mode} mode. Confirmations, results and what-ifs from the other mode were cleared; what you
            typed is kept. Confirm and optimize again.
          </p>
        )}
        {modeMismatch && state.kind === "ok" && (
          <div role="alert" data-testid="mode-mismatch" className="mb-4 rounded border border-red-300 bg-red-50 p-3 text-red-900">
            <p>
              Mode mismatch: this app is in {dataMode} mode but the backend reports {state.health.mode} mode. Results from
              this backend are refused until the two agree.
            </p>
            <p className="mt-1 text-sm">
              Switch the app to {state.health.mode} mode (nothing you typed is lost), or restart the backend with{" "}
              <code>APP_MODE={dataMode}</code> and check again. The app's starting mode comes from{" "}
              <code>VITE_DATA_MODE</code>.
            </p>
            <button
              type="button"
              onClick={switchMode}
              className="mt-2 min-h-8 rounded border border-red-700 bg-white px-3 py-1 text-sm font-medium text-red-900"
            >
              Switch the app to {state.health.mode} mode
            </button>
          </div>
        )}

        <div className="grid gap-6 lg:grid-cols-2">
          <section aria-labelledby="inputs-heading" className="min-w-0 rounded border border-slate-200 bg-white p-4">
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

          <section aria-labelledby="results-heading" className="min-w-0 rounded border border-slate-200 bg-white p-4">
            <h2 id="results-heading" className="text-lg font-medium">
              Results and backend
            </h2>
            <p className="mt-1 break-all text-sm text-slate-600" data-testid="api-base-url">
              {config.apiBaseUrl}
            </p>
            {scenario.result ? (
              <p className="mt-3 text-sm text-slate-700" data-testid="result-slot">
                A result for the current inputs is available.
              </p>
            ) : (
              <div className="mt-3">
                <EmptyState testId="result-slot">
                  No result. Results and certificates are cleared whenever the inputs change.
                </EmptyState>
              </div>
            )}
            <OptimizePanel
              client={client}
              confirmed={scenario.confirmed}
              version={scenario.version}
              result={scenario.result}
              onResult={(forVersion, result) => dispatch({ type: "set_result", forVersion, result })}
            />
            <Simulation
              client={client}
              confirmed={scenario.confirmed}
              baseResult={scenario.result}
              version={scenario.version}
              whatIf={scenario.whatIf}
              onWhatIf={(forVersion, whatIf) => dispatch({ type: "set_whatif", forVersion, whatIf })}
              onDiscard={() => dispatch({ type: "clear_whatif" })}
            />
            <div className="mt-3" data-testid="health-state" aria-live="polite">
              {state.kind === "checking" && <Pending>Checking /health…</Pending>}
              {state.kind === "ok" && (
                <>
                  <p className="text-emerald-900">
                    Backend OK. Schema v{state.health.schemaVersion}, mode <strong>{state.health.mode}</strong>, commit{" "}
                    <code>{state.health.commit}</code>.
                  </p>
                  <button
                    type="button"
                    onClick={() => setHealthAttempt((n) => n + 1)}
                    className="mt-1 min-h-8 rounded border border-slate-500 bg-white px-3 py-1 text-sm font-medium"
                  >
                    Check the backend again
                  </button>
                </>
              )}
              {state.kind === "unavailable" && (
                <AsyncError
                  error={state.error}
                  prefix="Backend unavailable."
                  onRetry={() => setHealthAttempt((n) => n + 1)}
                  testId="health-error"
                  focusOnMount={false}
                />
              )}
            </div>
          </section>
        </div>
      </main>
    </div>
  );
}
