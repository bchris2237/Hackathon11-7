// Editable scenario form state: text fields in, validated v1 Scenario out.
// Everything here is pure (no I/O, no globals mutated), so it is directly unit-testable.
// Internally all durations are integer minutes; the UI shows hours only when that is exact.
import type { Result, Scenario, Service } from "../../../contracts/models";
import canonicalFixture from "../../../fixtures/canonical.json";

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

/** Builds a fresh form from the fictional canonical fixture. Never returns shared references. */
export function loadCanonicalForm(): ScenarioForm {
  const scenario = structuredClone(canonicalFixture) as unknown as Scenario;
  return {
    businesses: scenario.businesses.map((b) => {
      const deliverable = scenario.deliverables.find((d) => d.ownerId === b.id);
      const estimates: Record<Service, string> = { coding: "", design: "", writing: "" };
      for (const e of b.estimates) estimates[e.service] = formatDuration(e.minutes);
      return {
        id: b.id,
        name: b.name,
        description: "",
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
}

export type ScenarioAction =
  | { type: "load_canonical" }
  | { type: "reset" }
  | { type: "edit_business"; index: number; patch: BusinessPatch }
  | { type: "set_result"; forVersion: number; result: Result };

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
  return { form: { businesses: [blankBusiness(), blankBusiness(), blankBusiness()] }, version: 0, result: null };
}

export function scenarioReducer(state: ScenarioState, action: ScenarioAction): ScenarioState {
  switch (action.type) {
    case "load_canonical":
      return { form: loadCanonicalForm(), version: state.version + 1, result: null };
    case "reset":
      return { ...initialState(), version: state.version + 1 };
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
      return { form: { businesses }, version: state.version + 1, result: null };
    }
    case "set_result":
      // A result computed for an older form version must never be shown.
      return action.forVersion === state.version ? { ...state, result: action.result } : state;
  }
}

export function errorsFor(errors: FieldError[], path: string): string | undefined {
  return errors.find((e) => e.path === path)?.message;
}
