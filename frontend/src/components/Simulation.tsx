import { useEffect, useId, useRef, useState } from "react";
import type { Change, DataMode, Result, Scenario, Service } from "../../../contracts/models";
import { ApiClient } from "../lib/api";
import { AsyncError, EmptyState, Pending, useLatestRequest } from "./AsyncState";
import {
  CHANGE_KINDS,
  MAX_WHATIF_TEXT,
  SERVICES,
  applyChanges,
  changeFromDraft,
  checkChanges,
  compareAssignments,
  describeChange,
  sameScenario,
  type ChangeKind,
  type WhatIf,
} from "../state/scenario";
import Certificate from "./Certificate";
import NetworkGraph, { describeMinutes } from "./NetworkGraph";
import { STATUS_TEXT } from "./OptimizePanel";
import Savings from "./Savings";
import ScrollRegion from "./ScrollRegion";

interface Props {
  client: ApiClient;
  /** Backend-confirmed scenario for the current input version. The base; never modified here. */
  confirmed: Scenario | null;
  /** Result for `confirmed`. Required: the what-if is always compared against it. */
  baseResult: Result | null;
  version: number;
  whatIf: WhatIf | null;
  onWhatIf: (forVersion: number, whatIf: Omit<WhatIf, "n" | "baseVersion">) => void;
  onDiscard: () => void;
}

type Proposal =
  | { source: "manual"; changes: Change[] }
  | { source: "text"; changes: Change[]; text: string; mode: DataMode };

type Status =
  | { kind: "idle" }
  | { kind: "proposing" }
  | { kind: "applying" }
  | { kind: "error"; error: unknown; context: "propose" | "apply" | "local" }
  | { kind: "notice"; message: string };

const FIELD =
  "mt-1 block w-full rounded border border-slate-400 bg-white px-2 py-1.5 text-slate-900 disabled:bg-slate-100";
const BUTTON =
  "rounded border border-slate-500 bg-white px-3 py-1.5 text-sm font-medium disabled:opacity-60 aria-disabled:cursor-not-allowed aria-disabled:opacity-60";

const cell = (totalsValue: number | undefined) => (totalsValue === undefined ? "n/a (infeasible)" : describeMinutes(totalsValue));

export default function Simulation({ client, confirmed, baseResult, version, whatIf, onWhatIf, onDiscard }: Props) {
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [status, setStatus] = useState<Status>({ kind: "idle" });
  const [text, setText] = useState("");
  const [kind, setKind] = useState<ChangeKind>("estimate");
  const [businessId, setBusinessId] = useState("");
  const [service, setService] = useState<Service>("design");
  const [value, setValue] = useState("");
  const [draftError, setDraftError] = useState<string | null>(null);
  const { start, invalidate } = useLatestRequest();
  const textId = useId();
  const resultHeadingId = useId();
  const draftErrorId = useId();
  // Focus targets: keyboard focus must never be left on an element that has just disappeared.
  const sectionHeading = useRef<HTMLHeadingElement>(null);
  const proposalHeading = useRef<HTMLHeadingElement>(null);
  const businessSelect = useRef<HTMLSelectElement>(null);
  const valueInput = useRef<HTMLInputElement>(null);
  const addButton = useRef<HTMLButtonElement>(null);
  const [draftErrorOn, setDraftErrorOn] = useState<"business" | "value">("value");
  const busy = status.kind === "proposing" || status.kind === "applying";

  // Any change of the inputs makes pending proposals and in-flight requests stale. What the
  // user typed (description, manual fields) is kept.
  // A switch between live and fixture mode replaces the client and does the same.
  useEffect(() => {
    invalidate();
    setProposal(null);
    setStatus({ kind: "idle" });
    setDraftError(null);
  }, [version, client, invalidate]);

  const ready = confirmed !== null && baseResult !== null;

  // A proposal that just arrived from the model is announced by moving focus to its heading.
  const proposalSource = proposal?.source;
  const proposalCount = proposal?.changes.length;
  useEffect(() => {
    if (proposalSource === "text") proposalHeading.current?.focus();
  }, [proposalSource, proposalCount]);

  async function propose() {
    if (!confirmed || !baseResult) return;
    const trimmed = text.trim();
    if (trimmed === "") return setStatus({ kind: "error", error: "Describe the change you want to try first.", context: "local" });
    if (trimmed.length > MAX_WHATIF_TEXT) {
      return setStatus({ kind: "error", error: `Keep the description under ${MAX_WHATIF_TEXT} characters.`, context: "local" });
    }
    const request = start();
    if (!request) return; // already running: no double submit
    setStatus({ kind: "proposing" });
    try {
      // Stage 1: text only, apply=false, no changes. The model can propose; it cannot change anything.
      const response = await client.simulate(
        { scenario: confirmed, changes: [], text: trimmed, apply: false },
        { signal: request.signal },
      );
      if (!request.isCurrent()) return;
      const problems = checkChanges(confirmed, response.proposedChanges);
      if (problems.length > 0) {
        setStatus({
          kind: "error",
          error: `The proposal was rejected and not shown as applicable. ${problems.join(" ")}`,
          context: "local",
        });
        return;
      }
      if (response.proposedChanges.length === 0) {
        setStatus({ kind: "notice", message: "No changes were proposed for that description. Try rephrasing it, or use the manual form." });
        return;
      }
      setProposal({ source: "text", changes: response.proposedChanges, text: trimmed, mode: response.mode });
      setStatus({ kind: "idle" });
    } catch (error) {
      if (!request.isCurrent() || (error instanceof DOMException && error.name === "AbortError")) return;
      setStatus({ kind: "error", error, context: "propose" });
    } finally {
      request.done();
    }
  }

  async function apply() {
    if (!proposal || !confirmed || !baseResult) return;
    const problems = checkChanges(confirmed, proposal.changes);
    if (problems.length > 0) return setStatus({ kind: "error", error: problems.join(" "), context: "local" });
    const request = start();
    if (!request) return; // already running: no double submit
    const forVersion = version;
    const approved = proposal.changes;
    setStatus({ kind: "applying" });
    try {
      // Stage 2: exactly the reviewed changes, apply=true, and no text.
      const response = await client.simulate(
        { scenario: confirmed, changes: approved, apply: true },
        { signal: request.signal },
      );
      if (!request.isCurrent()) return;
      if (response.result === null) {
        setStatus({
          kind: "error",
          error: "The backend returned no result for the applied changes, so nothing is shown.",
          context: "local",
        });
        return;
      }
      // The returned scenario must be exactly the base plus the approved changes, nothing more.
      if (!sameScenario(response.scenario, applyChanges(confirmed, approved))) {
        setStatus({
          kind: "error",
          error: "The returned scenario does not match your approved changes, so it was discarded.",
          context: "local",
        });
        return;
      }
      onWhatIf(forVersion, { changes: approved, scenario: response.scenario, result: response.result, mode: response.mode });
      setProposal(null);
      setStatus({ kind: "idle" });
    } catch (error) {
      if (!request.isCurrent() || (error instanceof DOMException && error.name === "AbortError")) return;
      // The proposal stays so it can be retried or cancelled.
      setStatus({ kind: "error", error, context: "apply" });
    } finally {
      request.done();
    }
  }

  function cancel() {
    invalidate();
    sectionHeading.current?.focus(); // the Cancel button is about to disappear
    setProposal(null);
    setStatus({ kind: "idle" });
  }

  function addManual() {
    if (!confirmed || busy) return;
    // On an error, focus goes to the field that needs fixing; its error text is its description.
    const fail = (message: string) => {
      const field = businessId === "" ? "business" : "value";
      setDraftErrorOn(field);
      setDraftError(message);
      (field === "business" ? businessSelect : valueInput).current?.focus();
    };
    const made = changeFromDraft({ kind, businessId, service, value });
    if (!made.ok) return fail(made.error);
    const next = [...(proposal?.changes ?? []), made.change];
    const problems = checkChanges(confirmed, next);
    if (problems.length > 0) return fail(problems[problems.length - 1]);
    setDraftError(null);
    setProposal({ source: "manual", changes: next });
    setValue("");
    setStatus({ kind: "idle" });
  }

  function removeManual(index: number) {
    if (proposal?.source !== "manual" || busy) return;
    const next = proposal.changes.filter((_, i) => i !== index);
    setProposal(next.length === 0 ? null : { source: "manual", changes: next });
    addButton.current?.focus(); // the button that was pressed is about to disappear
  }

  const textProposalOpen = proposal?.source === "text";

  return (
    <section aria-labelledby="whatif-heading" className="mt-6 space-y-4 border-t border-slate-200 pt-4" data-testid="simulation">
      <h3 id="whatif-heading" ref={sectionHeading} tabIndex={-1} className="text-base font-semibold focus:outline-none">
        What-if simulation
      </h3>
      <p className="text-sm text-slate-700">
        Try a change on a copy of the confirmed scenario. Nothing is applied until you review the exact changes and approve
        them. Your original scenario and result stay as they are.
      </p>
      {!ready && (
        <EmptyState testId="whatif-not-ready">
          Confirm and optimize the scenario first. A what-if is always compared with that result.
        </EmptyState>
      )}

      <fieldset disabled={!ready} className="min-w-0 space-y-3 rounded border border-slate-300 p-3">
        <legend className="px-1 text-sm font-semibold">Describe a change in words</legend>
        <div>
          <label htmlFor={textId} className="block text-sm font-medium">
            What would you like to try?
          </label>
          <textarea
            id={textId}
            rows={2}
            maxLength={MAX_WHATIF_TEXT}
            className={FIELD}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          <p className="mt-0.5 text-xs text-slate-600">The model only proposes changes. You review them before anything is applied.</p>
        </div>
        <button type="button" className={BUTTON} onClick={propose}>
          {status.kind === "proposing" ? "Proposing…" : "Propose changes"}
        </button>
      </fieldset>

      <fieldset disabled={!ready || textProposalOpen} className="min-w-0 space-y-3 rounded border border-slate-300 p-3">
        <legend className="px-1 text-sm font-semibold">Or enter a change yourself</legend>
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label htmlFor={`${textId}-b`} className="block text-sm font-medium">What-if business</label>
            <select
              id={`${textId}-b`}
              ref={businessSelect}
              className={FIELD}
              value={businessId}
              aria-invalid={draftError !== null && draftErrorOn === "business"}
              aria-describedby={draftError !== null && draftErrorOn === "business" ? draftErrorId : undefined}
              onChange={(e) => setBusinessId(e.target.value)}
            >
              <option value="">Choose…</option>
              {confirmed?.businesses.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor={`${textId}-k`} className="block text-sm font-medium">What-if change type</label>
            <select id={`${textId}-k`} className={FIELD} value={kind} onChange={(e) => setKind(e.target.value as ChangeKind)}>
              {CHANGE_KINDS.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </select>
          </div>
          {kind === "estimate" && (
            <div>
              <label htmlFor={`${textId}-s`} className="block text-sm font-medium">What-if service</label>
              <select id={`${textId}-s`} className={FIELD} value={service} onChange={(e) => setService(e.target.value as Service)}>
                {SERVICES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
          )}
          <div>
            <label htmlFor={`${textId}-v`} className="block text-sm font-medium">What-if new value (hours)</label>
            <input
              id={`${textId}-v`}
              ref={valueInput}
              inputMode="decimal"
              className={FIELD}
              value={value}
              aria-invalid={draftError !== null && draftErrorOn === "value"}
              aria-describedby={draftError !== null && draftErrorOn === "value" ? draftErrorId : undefined}
              onChange={(e) => setValue(e.target.value)}
            />
            <p className="mt-0.5 text-xs text-slate-600">Hours (2.5) or minutes (90m). Whole minutes only.</p>
          </div>
        </div>
        {draftError && (
          <p id={draftErrorId} role="alert" className="text-sm font-medium text-red-800">
            {draftError}
          </p>
        )}
        <button type="button" ref={addButton} className={BUTTON} onClick={addManual}>
          Add change
        </button>
      </fieldset>

      <div aria-live="polite">
        {status.kind === "proposing" && <Pending>Asking the model for a proposal…</Pending>}
        {status.kind === "applying" && <Pending>Recalculating with your approved changes…</Pending>}
        {status.kind === "error" && (
          <AsyncError
            error={status.error}
            prefix={status.context === "propose" ? "Proposal failed." : status.context === "apply" ? "Applying failed." : undefined}
            suffix={
              status.context === "propose"
                ? "Your description is kept."
                : status.context === "apply"
                  ? "Your changes are kept and nothing was applied."
                  : undefined
            }
            manualHint="You can still enter a change yourself with the manual form."
            onRetry={status.context === "propose" ? propose : status.context === "apply" ? apply : undefined}
            testId="whatif-error"
          />
        )}
        {status.kind === "notice" && (
          <p role="status" className="text-sm text-slate-800">
            {status.message}
          </p>
        )}
      </div>

      {proposal && confirmed && (
        <section aria-label="Proposed changes for review" className="rounded border-2 border-amber-500 bg-amber-50 p-3" data-testid="proposal">
          <h4 ref={proposalHeading} tabIndex={-1} className="text-sm font-semibold focus:outline-none">
            Review these changes. They are NOT applied yet.
          </h4>
          {proposal.source === "text" ? (
            <p className="mt-1 text-sm">
              Proposed from your text “{proposal.text}”{" "}
              {proposal.mode === "fixture" ? <strong className="text-amber-900">(SIMULATED, fixture mode, no model was used)</strong> : "(live)"}.
            </p>
          ) : (
            <p className="mt-1 text-sm">Entered by you.</p>
          )}
          <ol className="mt-2 list-decimal pl-5 text-sm" data-testid="proposal-changes">
            {proposal.changes.map((c, i) => (
              <li key={i}>
                {describeChange(confirmed, c)}
                {proposal.source === "manual" && (
                  <button
                    type="button"
                    className="ml-2 min-h-6 text-xs underline aria-disabled:opacity-60"
                    onClick={() => removeManual(i)}
                    aria-disabled={busy}
                    aria-label={`Remove change ${i + 1}`}
                  >
                    remove
                  </button>
                )}
              </li>
            ))}
          </ol>
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              className="rounded bg-blue-800 px-4 py-2 text-sm font-medium text-white aria-disabled:cursor-not-allowed aria-disabled:bg-slate-500"
              aria-disabled={busy}
              onClick={apply}
            >
              {status.kind === "applying" ? "Applying…" : "Apply these changes"}
            </button>
            <button type="button" className={BUTTON} onClick={cancel}>
              Cancel
            </button>
          </div>
        </section>
      )}
      {!proposal && busy && (
        <button type="button" className={BUTTON} onClick={cancel}>
          Cancel
        </button>
      )}

      {whatIf && confirmed && baseResult && (
        <WhatIfResult
          client={client}
          confirmed={confirmed}
          baseResult={baseResult}
          whatIf={whatIf}
          headingId={resultHeadingId}
          onDiscard={() => {
            onDiscard();
            sectionHeading.current?.focus(); // the Discard button is about to disappear
          }}
        />
      )}
    </section>
  );
}

function WhatIfResult({
  client,
  confirmed,
  baseResult,
  whatIf,
  headingId,
  onDiscard,
}: {
  client: ApiClient;
  confirmed: Scenario;
  baseResult: Result;
  whatIf: WhatIf;
  headingId: string;
  onDiscard: () => void;
}) {
  const { result } = whatIf;
  const heading = useRef<HTMLHeadingElement>(null);
  // The Apply button is gone once the result is shown, so focus moves to the result.
  useEffect(() => {
    heading.current?.focus();
  }, [whatIf.n]);
  const nameOf = (id: string | null) => (id === null ? "none" : (whatIf.scenario.businesses.find((b) => b.id === id)?.name ?? id));
  const rows = compareAssignments(whatIf.scenario, baseResult, result);
  const b = baseResult.totals ?? undefined;
  const w = result.totals ?? undefined;
  const metricRows: [string, number | undefined, number | undefined][] = [
    ["Production", b?.productionMinutes, w?.productionMinutes],
    ["Coordination", b?.coordinationMinutes, w?.coordinationMinutes],
    ["Effort", b?.effortMinutes, w?.effortMinutes],
    ["Gross savings", b?.grossSavingsMinutes, w?.grossSavingsMinutes],
    ["Net savings", b?.netSavingsMinutes, w?.netSavingsMinutes],
  ];

  return (
    <section aria-labelledby={headingId} className="space-y-3 rounded border-2 border-blue-700 p-3" data-testid="whatif-result">
      <h4 id={headingId} ref={heading} tabIndex={-1} className="text-base font-semibold focus:outline-none">
        What-if #{whatIf.n}, a modified copy of the confirmed scenario (input version {whatIf.baseVersion})
      </h4>
      <p className="text-sm">
        {whatIf.mode === "fixture" ? (
          <strong className="text-amber-900">SIMULATED (fixture mode). </strong>
        ) : null}
        The original scenario and its result above are unchanged.
      </p>

      <div>
        <h5 className="text-sm font-semibold">Changes applied</h5>
        <ol className="list-decimal pl-5 text-sm" data-testid="applied-changes">
          {whatIf.changes.map((c, i) => (
            <li key={i}>{describeChange(confirmed, c)}</li>
          ))}
        </ol>
      </div>

      <p className="text-base font-semibold">
        Outcome: <span data-testid="whatif-status">{STATUS_TEXT[result.status]}</span>
      </p>
      {result.reasons.length > 0 && (
        <ul className="list-disc rounded bg-slate-100 p-2 pl-6 text-sm" data-testid="whatif-reasons">
          {result.reasons.map((r) => (
            <li key={`${r.code}-${r.message}`}>
              <code>{r.code}</code>: {r.message}
            </li>
          ))}
        </ul>
      )}

      <ScrollRegion label="Original compared with what-if">
        <table className="w-full text-left text-sm" data-testid="comparison">
          <caption className="mb-1 text-left text-sm font-semibold">Original compared with what-if</caption>
          <thead>
            <tr className="border-b border-slate-300">
              <th scope="col" className="py-1 pr-2">Measure</th>
              <th scope="col" className="pr-2">Original (confirmed, version {whatIf.baseVersion})</th>
              <th scope="col">What-if #{whatIf.n}</th>
            </tr>
          </thead>
          <tbody>
            <tr className="border-b border-slate-200">
              <th scope="row" className="py-1 pr-2 font-medium">Status</th>
              <td className="pr-2">{STATUS_TEXT[baseResult.status]}</td>
              <td>{STATUS_TEXT[result.status]}</td>
            </tr>
            {metricRows.map(([label, before, after]) => (
              <tr key={label} className="border-b border-slate-200">
                <th scope="row" className="py-1 pr-2 font-medium">{label}</th>
                <td className="pr-2">{cell(before)}</td>
                <td>{cell(after)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </ScrollRegion>

      <ScrollRegion label="Who does each deliverable">
        <table className="w-full text-left text-sm" data-testid="assignment-diff">
          <caption className="mb-1 text-left text-sm font-semibold">Who does each deliverable</caption>
          <thead>
            <tr className="border-b border-slate-300">
              <th scope="col" className="py-1 pr-2">Deliverable</th>
              <th scope="col" className="pr-2">For</th>
              <th scope="col" className="pr-2">Original</th>
              <th scope="col" className="pr-2">What-if</th>
              <th scope="col">Change</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.deliverableId} className="border-b border-slate-200" data-changed={r.changed}>
                <th scope="row" className="py-1 pr-2 font-medium"><code>{r.deliverableId}</code> ({r.service})</th>
                <td className="pr-2">{nameOf(r.ownerId)}</td>
                <td className="pr-2">
                  {nameOf(r.beforeProvider)}
                  {r.beforeMinutes !== null && `, ${describeMinutes(r.beforeMinutes)}`}
                </td>
                <td className="pr-2">
                  {nameOf(r.afterProvider)}
                  {r.afterMinutes !== null && `, ${describeMinutes(r.afterMinutes)}`}
                </td>
                <td>{r.changed ? "changed" : "same"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </ScrollRegion>

      {result.status !== "infeasible" && result.totals && result.businessMetrics && (
        <NetworkGraph scenario={whatIf.scenario} result={result} previousResult={baseResult} />
      )}
      <Savings scenario={whatIf.scenario} result={result} client={client} />
      <Certificate result={result} mode={client.dataMode} />

      <button type="button" className={BUTTON} onClick={onDiscard}>
        Discard what-if
      </button>
    </section>
  );
}
