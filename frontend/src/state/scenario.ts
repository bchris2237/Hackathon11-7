// Editable scenario form state: text fields in, validated v1 Scenario out.
// Everything here is pure (no I/O, no globals mutated), so it is directly unit-testable.
// Internally all durations are integer minutes; the UI shows hours only when that is exact.
import type { Change, DataMode, Draft, Result, Scenario, Service } from "../../../contracts/models";
import canonicalFixture from "../../../fixtures/canonical.json";
import extractionFixture from "../../../fixtures/extraction.json";

export const SERVICES: readonly Service[] = ["coding", "design", "writing"];
export const MAX_MINUTES = 1_000_000;
const SLUG = /^[a-z0-9]+(?:[-_][a-z0-9]+)*$/; // same pattern as contracts/scenario.schema.json
const MAX_NAME = 200;
const MAX_DESCRIPTION = 4000;

export interface FormBusiness {
  id: string;
  name: string;
  /** Free-text description. Used for extraction later; not part of the Scenario wire type. */
  description: string;
  capacity: string;
  coordination: string;
  /** Blank means "unsupported service", never zero effort. */
  estimates: Record<Service, string>;
  /** The one deliverable this business needs (MVP: one per business). */
  requirement: { deliverableId: string; service: Service };
}

export interface ScenarioForm {
  businesses: FormBusiness[];
}

export interface FieldError {
  /** e.g. "businesses[0].capacity". */
  path: string;
  message: string;
}

// ---- duration parsing / formatting ------------------------------------------------------

export type DurationParse = { ok: true; minutes: number } | { ok: false; error: string };

/**
 * Parses "2.5" or "2.5h" (hours) and "90m" / "90 min" (minutes) into integer minutes.
 * Rejects anything that is not a whole number of minutes (sub-minute precision), so no
 * rounding ever happens before capacity or savings checks.
 */
export function parseDuration(text: string, kind: "positive" | "nonnegative"): DurationParse {
  const raw = text.trim().toLowerCase();
  if (raw === "") return { ok: false, error: "Required." };
  const match = /^(\d+)(?:\.(\d+))?\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)?$/.exec(raw);
  if (!match) return { ok: false, error: "Enter a non-negative number such as 2.5 (hours) or 90m (minutes)." };
  const [, whole, frac = "", unit = "h"] = match;
  const isMinutes = unit.startsWith("m");
  let minutes: number;
  if (isMinutes) {
    if (frac !== "") return { ok: false, error: "Minutes must be a whole number." };
    minutes = Number(whole);
  } else {
    if (frac.length > 6) return { ok: false, error: "Too many decimal places." };
    const scale = 10 ** frac.length;
    const fracMinutes = Number(frac || "0") * 60; // numerator over `scale`
    if (fracMinutes % scale !== 0) {
      return { ok: false, error: "Not a whole number of minutes (sub-minute precision is not allowed)." };
    }
    minutes = Number(whole) * 60 + fracMinutes / scale;
  }
  if (!Number.isSafeInteger(minutes) || minutes > MAX_MINUTES) {
    return { ok: false, error: `Must be at most ${MAX_MINUTES} minutes.` };
  }
  if (kind === "positive" && minutes < 1) return { ok: false, error: "Must be at least 1 minute." };
  return { ok: true, minutes };
}

/**
 * Display text for a minutes value. Hours are shown only when exactly representable as a
 * finite decimal (minutes divisible by 3); otherwise minutes are shown, never a rounded hour.
 */
export function formatDuration(minutes: number): string {
  if (minutes % 3 !== 0) return `${minutes}m`;
  const hundredths = (minutes * 100) / 60; // exact integer when minutes % 3 === 0
  const whole = Math.trunc(hundredths / 100);
  const frac = hundredths % 100;
  if (frac === 0) return String(whole);
  return `${whole}.${String(frac).padStart(2, "0").replace(/0$/, "")}`;
}

// ---- fixture loading --------------------------------------------------------------------

/**
 * Builds a fresh form from the fictional canonical fixture. Never returns shared references.
 * The three descriptions are the fictional texts the extraction fixture uses for the same businesses, so "Load fictional sample"
 * followed by "Extract draft" works without typing anything. They are not part of the Scenario wire type.
 */
export function loadCanonicalForm(): ScenarioForm {
  const scenario = structuredClone(canonicalFixture) as unknown as Scenario;
  const sampleDescriptions = new Map<string, string>(
    (extractionFixture.request.descriptions as { id: string; text: string }[]).map((d) => [d.id, d.text]),
  );
  return {
    businesses: scenario.businesses.map((b) => {
      const deliverable = scenario.deliverables.find((d) => d.ownerId === b.id);
      const estimates: Record<Service, string> = { coding: "", design: "", writing: "" };
      for (const e of b.estimates) estimates[e.service] = formatDuration(e.minutes);
      return {
        id: b.id,
        name: b.name,
        description: sampleDescriptions.get(b.id) ?? "",
        capacity: formatDuration(b.capacityMinutes),
        coordination: formatDuration(b.coordinationMinutes),
        estimates,
        requirement: {
          deliverableId: deliverable?.id ?? "",
          service: deliverable?.service ?? "coding",
        },
      };
    }),
  };
}

// ---- validation -------------------------------------------------------------------------

export interface Validation {
  errors: FieldError[];
  /** Non-null only when errors is empty. */
  scenario: Scenario | null;
}

export function validateForm(form: ScenarioForm): Validation {
  const errors: FieldError[] = [];
  const add = (path: string, message: string) => errors.push({ path, message });

  if (form.businesses.length !== 3) add("businesses", "The MVP needs exactly 3 businesses.");

  const seenBusinessIds = new Map<string, number>();
  const seenDeliverableIds = new Map<string, number>();
  const businesses: Scenario["businesses"] = [] as unknown as Scenario["businesses"];
  const deliverables: Scenario["deliverables"] = [] as unknown as Scenario["deliverables"];

  form.businesses.forEach((b, i) => {
    const p = `businesses[${i}]`;
    const id = b.id.trim();
    if (id === "") add(`${p}.id`, "ID is required.");
    else if (id.length > 64 || !SLUG.test(id)) add(`${p}.id`, "Use lowercase letters, digits, - or _ (e.g. devco).");
    else if (seenBusinessIds.has(id)) add(`${p}.id`, "ID must be unique.");
    seenBusinessIds.set(id, i);

    const name = b.name.trim();
    if (name === "") add(`${p}.name`, "Name is required.");
    else if (name.length > MAX_NAME) add(`${p}.name`, `Name must be at most ${MAX_NAME} characters.`);
    if (b.description.length > MAX_DESCRIPTION) add(`${p}.description`, `Keep it under ${MAX_DESCRIPTION} characters.`);

    const capacity = parseDuration(b.capacity, "nonnegative");
    if (!capacity.ok) add(`${p}.capacity`, capacity.error);
    const coordination = parseDuration(b.coordination, "nonnegative");
    if (!coordination.ok) add(`${p}.coordination`, coordination.error);

    const estimates: { service: Service; minutes: number }[] = [];
    for (const service of SERVICES) {
      if (b.estimates[service].trim() === "") continue; // unsupported
      const parsed = parseDuration(b.estimates[service], "positive");
      if (parsed.ok) estimates.push({ service, minutes: parsed.minutes });
      else add(`${p}.estimates.${service}`, parsed.error);
    }

    const requirement = b.requirement;
    if (!SERVICES.includes(requirement.service)) add(`${p}.requirement`, "Choose the service this business needs.");
    else if (b.estimates[requirement.service].trim() === "") {
      // Client-side mirror of the backend's 422 MISSING_BASELINE.
      add(`${p}.estimates.${requirement.service}`, "Required: the business's own estimate for what it needs defines its baseline.");
    }
    const deliverableId = requirement.deliverableId.trim();
    if (deliverableId === "") add(`${p}.deliverableId`, "Deliverable ID is required.");
    else if (deliverableId.length > 64 || !SLUG.test(deliverableId)) {
      add(`${p}.deliverableId`, "Use lowercase letters, digits, - or _ (e.g. d-design).");
    } else if (seenDeliverableIds.has(deliverableId)) add(`${p}.deliverableId`, "Deliverable ID must be unique.");
    seenDeliverableIds.set(deliverableId, i);

    if (capacity.ok && coordination.ok) {
      businesses.push({
        id,
        name,
        capacityMinutes: capacity.minutes,
        coordinationMinutes: coordination.minutes,
        estimates,
      });
    }
    deliverables.push({ id: deliverableId, ownerId: id, service: requirement.service });
  });

  if (errors.length > 0) return { errors, scenario: null };
  return {
    errors,
    scenario: { schemaVersion: "1", businesses, deliverables, confirmed: true },
  };
}

// ---- state container --------------------------------------------------------------------

export interface ScenarioState {
  form: ScenarioForm;
  /** Bumped on every edit/load/reset; used to drop late results. */
  version: number;
  /** Optimization result for the current form version. Cleared by every edit. */
  result: Result | null;
  /** Backend-confirmed scenario for the current form version. Cleared by every edit. */
  confirmed: Scenario | null;
  /** Latest applied what-if, kept next to (never instead of) `result`. Cleared by every edit. */
  whatIf: WhatIf | null;
  /** How many what-ifs were applied for the current form version; labels them #1, #2, ... */
  whatIfCount: number;
}

/** A what-if is computed on a COPY of the confirmed scenario; the original is never replaced. */
export interface WhatIf {
  /** 1-based label within the current input version. */
  n: number;
  /** Input version of the form this what-if was derived from. */
  baseVersion: number;
  changes: Change[];
  scenario: Scenario;
  result: Result;
  mode: DataMode;
}

export type ScenarioAction =
  | { type: "load_canonical" }
  | { type: "reset" }
  | { type: "apply_draft"; form: ScenarioForm }
  | { type: "set_confirmed"; forVersion: number; scenario: Scenario }
  | { type: "edit_business"; index: number; patch: BusinessPatch }
  | { type: "set_result"; forVersion: number; result: Result }
  | { type: "set_whatif"; forVersion: number; whatIf: Omit<WhatIf, "n" | "baseVersion"> }
  | { type: "clear_whatif" }
  | { type: "reset_derived" };

export type BusinessPatch = Partial<Omit<FormBusiness, "estimates" | "requirement">> & {
  estimates?: Partial<Record<Service, string>>;
  requirement?: Partial<FormBusiness["requirement"]>;
};

export function blankBusiness(): FormBusiness {
  return {
    id: "",
    name: "",
    description: "",
    capacity: "",
    coordination: "",
    estimates: { coding: "", design: "", writing: "" },
    requirement: { deliverableId: "", service: "coding" },
  };
}

export function initialState(): ScenarioState {
  return {
    form: { businesses: [blankBusiness(), blankBusiness(), blankBusiness()] },
    version: 0,
    result: null,
    confirmed: null,
    whatIf: null,
    whatIfCount: 0,
  };
}

/**
 * Turns an extraction draft into editable form state. Null/absent values become blank
 * fields (never 0), so validation keeps the scenario unconfirmable until the user fills them.
 * `descriptions` (id -> original text) is kept so the user can re-run extraction.
 */
export function draftToForm(draft: Draft, descriptions: Record<string, string>): ScenarioForm {
  const businesses = draft.businesses.slice(0, 3).map((b): FormBusiness => {
    const deliverable = draft.deliverables.find((d) => d.ownerId === b.id);
    const estimates: Record<Service, string> = { coding: "", design: "", writing: "" };
    for (const e of b.estimates ?? []) {
      if (SERVICES.includes(e.service)) estimates[e.service] = formatDuration(e.minutes);
    }
    return {
      id: b.id,
      name: b.name,
      description: descriptions[b.id] ?? "",
      capacity: b.capacityMinutes === null ? "" : formatDuration(b.capacityMinutes),
      coordination: b.coordinationMinutes === null ? "" : formatDuration(b.coordinationMinutes),
      estimates,
      requirement: { deliverableId: deliverable?.id ?? "", service: deliverable?.service ?? "coding" },
    };
  });
  while (businesses.length < 3) businesses.push(blankBusiness());
  return { businesses };
}

/** Everything derived from the inputs, cleared together by any change to them. */
const CLEARED = { result: null, confirmed: null, whatIf: null, whatIfCount: 0 } as const;

export function scenarioReducer(state: ScenarioState, action: ScenarioAction): ScenarioState {
  switch (action.type) {
    case "load_canonical":
      return { form: loadCanonicalForm(), version: state.version + 1, ...CLEARED };
    case "reset":
      return { ...initialState(), version: state.version + 1 };
    case "apply_draft":
      return { form: action.form, version: state.version + 1, ...CLEARED };
    case "set_confirmed":
      // Only the exact form version that was sent can become confirmed.
      return action.forVersion === state.version ? { ...state, confirmed: action.scenario } : state;
    case "edit_business": {
      const current = state.form.businesses[action.index];
      if (!current) return state;
      const { estimates, requirement, ...rest } = action.patch;
      const next: FormBusiness = {
        ...current,
        ...rest,
        estimates: { ...current.estimates, ...estimates },
        requirement: { ...current.requirement, ...requirement },
      };
      const businesses = state.form.businesses.map((b, i) => (i === action.index ? next : b));
      // Any edit invalidates the prior result and its certificate.
      return { form: { businesses }, version: state.version + 1, ...CLEARED };
    }
    case "set_result":
      // A result computed for an older form version must never be shown.
      // A new base result also makes any earlier what-if comparison stale.
      return action.forVersion === state.version
        ? { ...state, result: action.result, whatIf: null, whatIfCount: 0 }
        : state;
    case "set_whatif": {
      // Only for the input version it was computed from, and only with a base result to compare to.
      if (action.forVersion !== state.version || !state.result) return state;
      const n = state.whatIfCount + 1;
      return { ...state, whatIfCount: n, whatIf: { ...action.whatIf, n, baseVersion: state.version } };
    }
    case "clear_whatif":
      return { ...state, whatIf: null };
    case "reset_derived":
      // Used when the data mode is switched: keep what the user typed, drop everything that
      // was produced under the other mode (confirmation, result, what-if) and invalidate requests.
      return { ...state, version: state.version + 1, ...CLEARED };
  }
}

export function errorsFor(errors: FieldError[], path: string): string | undefined {
  return errors.find((e) => e.path === path)?.message;
}

// ---- what-if changes (allowlisted, pure) --------------------------------------------------

export const MAX_CHANGES = 20;
export const MAX_WHATIF_TEXT = 2000;
export type ChangeKind = Change["kind"];
export const CHANGE_KINDS: readonly ChangeKind[] = ["capacity", "coordination", "estimate"];

/** "3 h", "0.25 h" or "10 min": exact, never rounded. */
export function minutesLabel(minutes: number): string {
  const text = formatDuration(minutes);
  return text.endsWith("m") ? `${minutes} min` : `${text} h`;
}

const hasOnly = (value: object, keys: string[]) =>
  Object.keys(value).length === keys.length && keys.every((k) => k in value);

/** Strict structural check: only the three tagged shapes, no extra keys. */
export function isChange(value: unknown): value is Change {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const c = value as Record<string, unknown>;
  if (c.kind === "capacity" || c.kind === "coordination") {
    return hasOnly(c, ["kind", "businessId", "minutes"]) && typeof c.businessId === "string" && Number.isInteger(c.minutes);
  }
  if (c.kind === "estimate") {
    return (
      hasOnly(c, ["kind", "businessId", "service", "minutes"]) &&
      typeof c.businessId === "string" &&
      SERVICES.includes(c.service as Service) &&
      Number.isInteger(c.minutes)
    );
  }
  return false;
}

const targetKey = (c: Change) => (c.kind === "estimate" ? `estimate:${c.businessId}:${c.service}` : `${c.kind}:${c.businessId}`);

/**
 * Everything wrong with a list of changes against a scenario; empty means acceptable.
 * Used for manual input and, above all, for proposals coming back from the model: ids must
 * already exist, values must be in range and each field may be changed only once.
 */
export function checkChanges(scenario: Scenario, changes: unknown): string[] {
  if (!Array.isArray(changes)) return ["The changes are not a list."];
  if (changes.length > MAX_CHANGES) return [`At most ${MAX_CHANGES} changes are allowed.`];
  const problems: string[] = [];
  const seen = new Set<string>();
  changes.forEach((c, i) => {
    const at = `Change ${i + 1}`;
    if (!isChange(c)) {
      problems.push(`${at} is not an allowed change (capacity, coordination or estimate only).`);
      return;
    }
    if (!scenario.businesses.some((b) => b.id === c.businessId)) {
      problems.push(`${at} names an unknown business "${c.businessId}".`);
    }
    const min = c.kind === "estimate" ? 1 : 0;
    if (c.minutes < min || c.minutes > MAX_MINUTES) {
      problems.push(`${at} has ${c.minutes} minutes; it must be ${min} to ${MAX_MINUTES}.`);
    }
    const key = targetKey(c);
    if (seen.has(key)) problems.push(`${at} changes the same field as an earlier change.`);
    seen.add(key);
  });
  return problems;
}

/** Applies already-checked changes to a deep copy. The input scenario is never touched. */
export function applyChanges(scenario: Scenario, changes: readonly Change[]): Scenario {
  const copy = structuredClone(scenario);
  for (const c of changes) {
    const business = copy.businesses.find((b) => b.id === c.businessId);
    if (!business) continue;
    if (c.kind === "estimate") {
      const estimate = business.estimates.find((e) => e.service === c.service);
      if (estimate) estimate.minutes = c.minutes;
      else business.estimates.push({ service: c.service, minutes: c.minutes });
    } else if (c.kind === "capacity") business.capacityMinutes = c.minutes;
    else business.coordinationMinutes = c.minutes;
  }
  return copy;
}

/** Equality that ignores the order of a business's estimates, which carries no meaning. */
export function sameScenario(a: Scenario, b: Scenario): boolean {
  const norm = (s: Scenario) =>
    JSON.stringify({
      ...s,
      businesses: s.businesses.map((x) => ({
        id: x.id,
        name: x.name,
        capacityMinutes: x.capacityMinutes,
        coordinationMinutes: x.coordinationMinutes,
        estimates: [...x.estimates].sort((p, q) => p.service.localeCompare(q.service)).map((e) => [e.service, e.minutes]),
      })),
      deliverables: s.deliverables.map((d) => [d.id, d.ownerId, d.service]),
    });
  return norm(a) === norm(b);
}

/** One reviewable line, e.g. "DesignCo design estimate: 3 h → 10 h". Old value comes from the scenario. */
export function describeChange(scenario: Scenario, c: Change): string {
  const business = scenario.businesses.find((b) => b.id === c.businessId);
  const name = business?.name ?? c.businessId;
  const to = minutesLabel(c.minutes);
  if (c.kind === "estimate") {
    const old = business?.estimates.find((e) => e.service === c.service)?.minutes;
    return `${name} ${c.service} estimate: ${old === undefined ? "unsupported" : minutesLabel(old)} → ${to}`;
  }
  const old = c.kind === "capacity" ? business?.capacityMinutes : business?.coordinationMinutes;
  return `${name} ${c.kind}: ${old === undefined ? "unknown" : minutesLabel(old)} → ${to}`;
}

export interface ChangeDraft {
  kind: ChangeKind;
  businessId: string;
  service: Service;
  /** Hours (or minutes with "m"), as typed. */
  value: string;
}

/** Turns what the user typed into a Change, or says why not. Same duration rules as the main form. */
export function changeFromDraft(
  draft: ChangeDraft,
): { ok: true; change: Change } | { ok: false; error: string } {
  if (!CHANGE_KINDS.includes(draft.kind)) return { ok: false, error: "Choose what to change." };
  if (draft.businessId === "") return { ok: false, error: "Choose a business." };
  const parsed = parseDuration(draft.value, draft.kind === "estimate" ? "positive" : "nonnegative");
  if (!parsed.ok) return { ok: false, error: parsed.error };
  if (draft.kind === "estimate") {
    if (!SERVICES.includes(draft.service)) return { ok: false, error: "Choose a service." };
    return { ok: true, change: { kind: "estimate", businessId: draft.businessId, service: draft.service, minutes: parsed.minutes } };
  }
  return { ok: true, change: { kind: draft.kind, businessId: draft.businessId, minutes: parsed.minutes } };
}

export interface AssignmentRow {
  deliverableId: string;
  ownerId: string;
  service: Service;
  beforeProvider: string | null;
  afterProvider: string | null;
  beforeMinutes: number | null;
  afterMinutes: number | null;
  changed: boolean;
}

/**
 * Per-deliverable before/after rows taken only from the two results. A missing assignment
 * (infeasible result) is null, never a guess. `changed` means provider or minutes differ.
 */
export function compareAssignments(scenario: Scenario, before: Result, after: Result): AssignmentRow[] {
  return scenario.deliverables.map((d) => {
    const b = before.assignments.find((a) => a.deliverableId === d.id);
    const a = after.assignments.find((x) => x.deliverableId === d.id);
    return {
      deliverableId: d.id,
      ownerId: d.ownerId,
      service: d.service,
      beforeProvider: b?.providerId ?? null,
      afterProvider: a?.providerId ?? null,
      beforeMinutes: b?.minutes ?? null,
      afterMinutes: a?.minutes ?? null,
      changed: (b?.providerId ?? null) !== (a?.providerId ?? null) || (b?.minutes ?? null) !== (a?.minutes ?? null),
    };
  });
}
