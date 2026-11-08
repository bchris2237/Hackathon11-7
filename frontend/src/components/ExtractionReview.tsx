import { useEffect, useState } from "react";
import type { DraftBusiness, ExtractRequest, ExtractResponse, Scenario } from "../../../contracts/models";
import { ApiClient } from "../lib/api";
import { AsyncError, Pending, useLatestRequest } from "./AsyncState";
import {
  SERVICES,
  draftToForm,
  formatDuration,
  type ScenarioForm,
  type Validation,
} from "../state/scenario";

interface Props {
  client: ApiClient;
  form: ScenarioForm;
  /** Bumped by the parent on every form change; in-flight requests for older versions are dropped. */
  version: number;
  validation: Validation;
  confirmed: Scenario | null;
  onApplyDraft: (form: ScenarioForm) => void;
  onConfirmed: (forVersion: number, scenario: Scenario) => void;
}

interface SentDescription {
  id: string;
  name: string;
  text: string;
}

interface Review {
  response: ExtractResponse;
  sent: SentDescription[];
}

type Status = { kind: "idle" } | { kind: "pending" } | { kind: "error"; error: unknown };

const SLUG = /^[a-z0-9]+(?:[-_][a-z0-9]+)*$/;

const hours = (minutes: number) => {
  const text = formatDuration(minutes);
  return text.endsWith("m") ? text.replace("m", " min") : `${text} h`;
};

/**
 * Plain-language version of a form error path such as "businesses[2].coordination". The technical path stays visible next to it
 * (support and tests use it); this only says, in words, which business and which box, and what a person can do about it.
 */
function describeBlocker(path: string, message: string, form: ScenarioForm): string {
  const m = /^businesses\[(\d+)\]\.(\w+)/.exec(path);
  if (!m) return message;
  const business = form.businesses[Number(m[1])];
  const who = business?.name.trim() || business?.id.trim() || `Business ${Number(m[1]) + 1}`;
  const box: Record<string, string> = {
    id: "ID",
    name: "name",
    capacity: "capacity (hours)",
    coordination: "coordination (hours)",
    deliverableId: "deliverable ID",
    estimates: "estimates",
  };
  const label = box[m[2]] ?? m[2];
  if (m[2] === "coordination" && /required/i.test(message)) {
    return `${who}: fill in ${label}. Its description does not say how long coordinating takes. Type 0 if it costs no extra time.`;
  }
  return `${who}, ${label}: ${message}`;
}

function Missing({ children }: { children?: React.ReactNode }) {  return (
    <span className="inline-block max-w-full rounded bg-red-100 px-1.5 py-0.5 text-xs font-semibold text-red-900" data-testid="missing-badge">
      MISSING{children ? `: ${children}` : ""}
    </span>
  );
}

function ProposedFields({ business, ownService }: { business: DraftBusiness; ownService: string | undefined }) {
  const estimate = (service: string) => business.estimates?.find((e) => e.service === service)?.minutes;
  return (
    <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1 text-sm [&_dd]:min-w-0">
      <dt className="font-medium">Capacity</dt>
      <dd>{business.capacityMinutes === null ? <Missing /> : hours(business.capacityMinutes)}</dd>
      <dt className="font-medium">Coordination</dt>
      <dd>{business.coordinationMinutes === null ? <Missing /> : hours(business.coordinationMinutes)}</dd>
      {SERVICES.map((service) => {
        const minutes = estimate(service);
        return (
          <div key={service} className="contents">
            <dt className="font-medium">{service} estimate</dt>
            <dd>
              {minutes !== undefined ? (
                hours(minutes)
              ) : service === ownService ? (
                <Missing>needed to define the baseline</Missing>
              ) : (
                <span className="text-slate-600">unsupported</span>
              )}
            </dd>
          </div>
        );
      })}
      <dt className="font-medium">Needs</dt>
      <dd>{ownService ?? <Missing />}</dd>
    </dl>
  );
}

export default function ExtractionReview({ client, form, version, validation, confirmed, onApplyDraft, onConfirmed }: Props) {
  const [review, setReview] = useState<Review | null>(null);
  const [extractStatus, setExtractStatus] = useState<Status>({ kind: "idle" });
  const [confirmStatus, setConfirmStatus] = useState<Status>({ kind: "idle" });
  const { start, invalidate } = useLatestRequest();

  // Any change to the form invalidates in-flight requests: abort them and bump the id so
  // that even a response that slips through cannot be applied.
  useEffect(() => {
    invalidate();
    setExtractStatus((s) => (s.kind === "pending" ? { kind: "idle" } : s));
    setConfirmStatus((s) => (s.kind === "idle" ? s : { kind: "idle" }));
  }, [version, invalidate]);

  // Switching between live and fixture mode replaces the client: nothing extracted under the
  // other mode may stay on screen.
  useEffect(() => {
    invalidate();
    setReview(null);
    setExtractStatus({ kind: "idle" });
    setConfirmStatus({ kind: "idle" });
  }, [client, invalidate]);

  async function runExtract() {
    if (extractStatus.kind === "pending" || confirmStatus.kind === "pending") return; // button is aria-disabled, not removed
    const sent = form.businesses
      .map((b) => ({ id: b.id.trim(), name: b.name.trim(), text: b.description.trim() }))
      .filter((d) => d.id !== "" && d.name !== "" && d.text !== "");
    if (sent.length === 0) {
      setExtractStatus({ kind: "error", error: "Enter an ID, a name and a description for at least one business first." });
      return;
    }
    const badId = sent.find((d) => !SLUG.test(d.id));
    if (badId) {
      setExtractStatus({ kind: "error", error: `Business ID "${badId.id}" is not a valid lowercase slug.` });
      return;
    }
    const request = start();
    if (!request) return; // a request is already running: no double submit
    setExtractStatus({ kind: "pending" });
    setConfirmStatus({ kind: "idle" });
    try {
      const response = await client.extract({ descriptions: sent } as ExtractRequest, { signal: request.signal });
      if (!request.isCurrent()) return; // superseded by newer input
      const texts = Object.fromEntries(sent.map((d) => [d.id, d.text]));
      setReview({ response, sent });
      setExtractStatus({ kind: "idle" });
      // Nothing is confirmed: the draft only fills the editable form, with blanks where null.
      onApplyDraft(draftToForm(response.draft, texts));
    } catch (error) {
      if (!request.isCurrent() || (error instanceof DOMException && error.name === "AbortError")) return;
      // The user's form is untouched. No fallback data is substituted.
      setExtractStatus({ kind: "error", error });
    } finally {
      request.done();
    }
  }

  async function runConfirm() {
    if (!validation.scenario) return;
    const request = start();
    if (!request) return;
    const forVersion = version;
    setConfirmStatus({ kind: "pending" });
    try {
      const response = await client.confirm({ scenario: validation.scenario }, { signal: request.signal });
      if (!request.isCurrent()) return;
      setConfirmStatus({ kind: "idle" });
      onConfirmed(forVersion, response.scenario);
    } catch (error) {
      if (!request.isCurrent() || (error instanceof DOMException && error.name === "AbortError")) return;
      setConfirmStatus({ kind: "error", error });
    } finally {
      request.done();
    }
  }

  const draft = review?.response.draft;
  const provenance = review?.response.provenance ?? [];

  return (
    <div className="mt-4 space-y-4">
      <div className="rounded border border-slate-300 p-3">
        <h3 className="text-base font-semibold">Extract a draft from the descriptions</h3>
        <p className="mt-1 text-sm text-slate-700">
          Sends each business description to the backend. The proposal replaces the fields above; nothing is confirmed
          until you do it.
        </p>
        <button
          type="button"
          onClick={runExtract}
          aria-disabled={extractStatus.kind === "pending" || confirmStatus.kind === "pending"}
          className="mt-2 rounded border border-slate-500 bg-white px-3 py-1.5 text-sm font-medium aria-disabled:cursor-not-allowed aria-disabled:opacity-60"
        >
          {extractStatus.kind === "pending" ? "Extracting…" : "Extract draft"}
        </button>
        {extractStatus.kind === "pending" && <Pending>Reading the descriptions…</Pending>}
        {extractStatus.kind === "error" && (
          <AsyncError
            error={extractStatus.error}
            prefix="Extraction failed."
            suffix="Your inputs are unchanged."
            manualHint="You can still type the values into the fields above by hand."
            onRetry={runExtract}
            testId="extract-error"
          />
        )}
      </div>

      {review && draft && (
        <section aria-labelledby="review-heading" className="rounded border border-slate-300 p-3" data-testid="extraction-review">
          <h3 id="review-heading" className="text-base font-semibold">
            Review the proposed draft
          </h3>
          <p className="mt-1 text-sm">
            Source:{" "}
            {review.response.mode === "fixture" ? (
              <strong className="text-amber-900">SIMULATED (fixture) extraction, no model was used</strong>
            ) : (
              <strong>live extraction</strong>
            )}
            . This is an unconfirmed draft.
          </p>
          {review.response.missingFields.length > 0 && (
            <div role="alert" className="mt-2 rounded border border-red-300 bg-red-50 p-2 text-sm text-red-900">
              <p className="font-medium">The extractor could not find these values. Fill them in above:</p>
              <ul className="list-disc pl-5" data-testid="missing-fields">
                {review.response.missingFields.map((f) => (
                  <li key={f}>
                    <code>{f}</code>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <div className="mt-3 space-y-4">
            {draft.businesses.map((business, index) => {
              const own = draft.deliverables.find((d) => d.ownerId === business.id);
              const quotes = provenance.filter(
                (p) =>
                  p.path.startsWith(`businesses[${index}].`) ||
                  draft.deliverables.some((d, di) => d.ownerId === business.id && p.path === `deliverables[${di}]`),
              );
              const original = review.sent.find((d) => d.id === business.id)?.text;
              return (
                <article key={business.id} className="grid grid-cols-1 gap-3 rounded bg-slate-50 p-3 [&>div]:min-w-0" aria-label={`Review of ${business.name}`}>
                  <div>
                    <h4 className="text-sm font-semibold">{business.name}: original text</h4>
                    <p className="mt-1 whitespace-pre-wrap text-sm text-slate-800">{original ?? "(not sent)"}</p>
                  </div>
                  <div>
                    <h4 className="text-sm font-semibold">Proposed fields</h4>
                    <div className="mt-1">
                      <ProposedFields business={business} ownService={own?.service} />
                    </div>
                  </div>
                  <div>
                    <h4 className="text-sm font-semibold">Supporting quotes</h4>
                    {quotes.length === 0 ? (
                      <p className="mt-1 text-sm text-slate-600">None provided.</p>
                    ) : (
                      <ul className="mt-1 space-y-1 text-sm">
                        {quotes.map((q) => (
                          <li key={`${q.path}-${q.quote}`}>
                            <code className="break-all text-xs text-slate-600">{q.path}</code>
                            <blockquote className="border-l-2 border-slate-400 pl-2">{q.quote}</blockquote>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                </article>
              );
            })}
          </div>
        </section>
      )}

      <div className="rounded border border-slate-300 p-3">
        <h3 className="text-base font-semibold">Confirm</h3>
        {validation.scenario ? (
          <p className="mt-1 text-sm text-emerald-900">All required values are present.</p>
        ) : (
          <div className="mt-1 text-sm text-red-800" data-testid="confirm-blockers">
            <p>Cannot confirm yet:</p>
            <ul className="list-disc pl-5">
              {validation.errors.slice(0, 8).map((e) => (
                <li key={e.path}>
                  <span data-testid="blocker-plain">{describeBlocker(e.path, e.message, form)}</span>{" "}
                  <code className="text-xs text-slate-600">({e.path})</code>
                </li>
              ))}
            </ul>
            {validation.errors.length > 8 && <p>…and {validation.errors.length - 8} more.</p>}
          </div>
        )}
        <button
          type="button"
          disabled={!validation.scenario}
          aria-disabled={confirmStatus.kind === "pending" || extractStatus.kind === "pending"}
          onClick={runConfirm}
          className="mt-2 rounded bg-blue-800 px-4 py-2 font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-400 aria-disabled:cursor-not-allowed aria-disabled:bg-slate-500"
        >
          {confirmStatus.kind === "pending" ? "Confirming…" : "Confirm scenario"}
        </button>
        {confirmStatus.kind === "pending" && <Pending>Checking the scenario…</Pending>}
        {confirmStatus.kind === "error" && (
          <AsyncError
            error={confirmStatus.error}
            prefix="Confirmation failed."
            suffix="Your inputs are unchanged."
            onRetry={runConfirm}
            testId="confirm-error"
          />
        )}
        {confirmed && (
          <p role="status" data-testid="confirmed-note" className="mt-2 text-sm font-medium text-emerald-900">
            Scenario confirmed by the backend. Editing any field withdraws the confirmation.
          </p>
        )}
      </div>
    </div>
  );
}
