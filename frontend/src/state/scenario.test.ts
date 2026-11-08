import { describe, expect, it } from "vitest";
import canonical from "../../../fixtures/canonical.json";
import disruption from "../../../fixtures/disruption.json";
import type { Change, Result, Scenario } from "../../../contracts/models";
import {
  applyChanges,
  changeFromDraft,
  checkChanges,
  compareAssignments,
  describeChange,
  isChange,
  minutesLabel,
  sameScenario,
  formatDuration,
  initialState,
  loadCanonicalForm,
  parseDuration,
  scenarioReducer,
  validateForm,
} from "./scenario";

const fakeResult = { schemaVersion: "1", status: "no_exchange" } as unknown as Result;

describe("parseDuration", () => {
  it.each([
    ["3", 180],
    ["2.5", 150],
    ["0.25h", 15],
    ["0.05", 3],
    ["0.1", 6],
    ["90m", 90],
    ["90 min", 90],
    ["0", 0],
    ["16666.6", 999996],
  ])("%s -> %i minutes", (text, minutes) => {
    expect(parseDuration(text, "nonnegative")).toEqual({ ok: true, minutes });
  });

  it.each(["", "abc", "-1", "1e3", "0.001", "0.017", "1.5m", "1,5", "1000000m1", "16667"])(
    "rejects %j",
    (text) => {
      expect(parseDuration(text, "nonnegative").ok).toBe(false);
    },
  );

  it("accepts exactly the maximum and rejects one over", () => {
    expect(parseDuration("1000000m", "nonnegative")).toEqual({ ok: true, minutes: 1_000_000 });
    expect(parseDuration("1000001m", "nonnegative").ok).toBe(false);
  });

  it("rejects zero for estimates (missing means unsupported, never instantaneous)", () => {
    expect(parseDuration("0", "positive").ok).toBe(false);
    expect(parseDuration("1m", "positive")).toEqual({ ok: true, minutes: 1 });
  });
});

describe("formatDuration", () => {
  it("shows exact hours, else minutes, and round-trips for every minute value", () => {
    expect(formatDuration(180)).toBe("3");
    expect(formatDuration(150)).toBe("2.5");
    expect(formatDuration(15)).toBe("0.25");
    expect(formatDuration(3)).toBe("0.05");
    expect(formatDuration(10)).toBe("10m");
    for (let m = 0; m <= 2000; m++) expect(parseDuration(formatDuration(m), "nonnegative")).toEqual({ ok: true, minutes: m });
  });
});

describe("canonical loader", () => {
  it("loads 3 businesses with 3 requirements and builds exactly the canonical scenario", () => {
    const form = loadCanonicalForm();
    expect(form.businesses).toHaveLength(3);
    expect(form.businesses.map((b) => b.requirement.service)).toEqual(["design", "writing", "coding"]);
    const { errors, scenario } = validateForm(form);
    expect(errors).toEqual([]);
    // Estimate order is not meaningful (the form emits coding, design, writing); compare sorted.
    const sortEstimates = (s: any) => ({
      ...s,
      businesses: s.businesses.map((b: any) => ({
        ...b,
        estimates: [...b.estimates].sort((x: any, y: any) => x.service.localeCompare(y.service)),
      })),
    });
    expect(sortEstimates(scenario)).toEqual(sortEstimates(canonical));
  });

  it("returns independent copies and never mutates the shared sample", () => {
    const before = JSON.stringify(canonical);
    const state = scenarioReducer(initialState(), { type: "load_canonical" });
    scenarioReducer(state, { type: "edit_business", index: 0, patch: { capacity: "1", estimates: { coding: "9" } } });
    expect(JSON.stringify(canonical)).toBe(before);
    expect(loadCanonicalForm().businesses[0].capacity).toBe("10");
  });
});

describe("validateForm", () => {
  const loaded = () => scenarioReducer(initialState(), { type: "load_canonical" });

  it("blank form is invalid and yields no scenario", () => {
    const v = validateForm(initialState().form);
    expect(v.scenario).toBeNull();
    expect(v.errors.some((e) => e.path === "businesses[0].id")).toBe(true);
  });

  it("rejects blank and malformed IDs and duplicate IDs", () => {
    let s = loaded();
    s = scenarioReducer(s, { type: "edit_business", index: 0, patch: { id: "  " } });
    expect(validateForm(s.form).errors.map((e) => e.path)).toContain("businesses[0].id");
    s = scenarioReducer(s, { type: "edit_business", index: 0, patch: { id: "Dev Co" } });
    expect(validateForm(s.form).scenario).toBeNull();
    s = scenarioReducer(s, { type: "edit_business", index: 0, patch: { id: "designco" } });
    expect(validateForm(s.form).errors.find((e) => e.message.includes("unique"))).toBeDefined();
  });

  it("rejects invalid numbers and sub-minute precision with field-level errors", () => {
    let s = loaded();
    s = scenarioReducer(s, { type: "edit_business", index: 1, patch: { capacity: "0.001", coordination: "x" } });
    const paths = validateForm(s.form).errors.map((e) => e.path);
    expect(paths).toContain("businesses[1].capacity");
    expect(paths).toContain("businesses[1].coordination");
  });

  it("requires the owner's own estimate (client mirror of MISSING_BASELINE)", () => {
    let s = loaded();
    s = scenarioReducer(s, { type: "edit_business", index: 0, patch: { estimates: { design: "" } } });
    const v = validateForm(s.form);
    expect(v.scenario).toBeNull();
    expect(v.errors.map((e) => e.path)).toContain("businesses[0].estimates.design");
  });

  it("treats a blank non-required estimate as unsupported, not zero", () => {
    let s = loaded();
    s = scenarioReducer(s, { type: "edit_business", index: 0, patch: { estimates: { coding: "" } } });
    const v = validateForm(s.form);
    expect(v.scenario?.businesses[0].estimates).toEqual([{ service: "design", minutes: 480 }]);
  });

  it("converts hours to integer minutes in the scenario", () => {
    let s = loaded();
    s = scenarioReducer(s, { type: "edit_business", index: 1, patch: { coordination: "1" } });
    expect(validateForm(s.form).scenario?.businesses[1].coordinationMinutes).toBe(60);
  });
});

describe("result invalidation", () => {
  it("every edit clears the prior result and bumps the version", () => {
    let s = scenarioReducer(initialState(), { type: "load_canonical" });
    s = scenarioReducer(s, { type: "set_result", forVersion: s.version, result: fakeResult });
    expect(s.result).not.toBeNull();
    const edited = scenarioReducer(s, { type: "edit_business", index: 0, patch: { name: "Other" } });
    expect(edited.result).toBeNull();
    expect(edited.version).toBe(s.version + 1);
  });

  it("load and reset also clear the result", () => {
    let s = scenarioReducer(initialState(), { type: "load_canonical" });
    s = scenarioReducer(s, { type: "set_result", forVersion: s.version, result: fakeResult });
    expect(scenarioReducer(s, { type: "reset" }).result).toBeNull();
    expect(scenarioReducer(s, { type: "load_canonical" }).result).toBeNull();
  });

  it("ignores a late result computed for an older version", () => {
    let s = scenarioReducer(initialState(), { type: "load_canonical" });
    const staleVersion = s.version;
    s = scenarioReducer(s, { type: "edit_business", index: 0, patch: { name: "Changed" } });
    expect(scenarioReducer(s, { type: "set_result", forVersion: staleVersion, result: fakeResult }).result).toBeNull();
  });
});

// ---- C6: what-if helpers and state --------------------------------------------------------

const base = canonical as unknown as Scenario;
const noExchangeChanges = disruption.noExchange.changes as Change[];
const infeasibleChanges = disruption.infeasible.changes as Change[];

describe("isChange / checkChanges", () => {
  it("accepts the fixture disruptions against the canonical scenario", () => {
    for (const c of [...noExchangeChanges, ...infeasibleChanges]) expect(isChange(c)).toBe(true);
    expect(checkChanges(base, noExchangeChanges)).toEqual([]);
    expect(checkChanges(base, infeasibleChanges)).toEqual([]);
  });

  it.each([
    [null],
    ["capacity"],
    [{ kind: "capacity", businessId: "devco" }],
    [{ kind: "capacity", businessId: "devco", minutes: "5" }],
    [{ kind: "estimate", businessId: "devco", minutes: 5 }],
    [{ kind: "estimate", businessId: "devco", service: "cooking", minutes: 5 }],
    [{ kind: "capacity", businessId: "devco", minutes: 5, extra: true }],
    [{ kind: "rm", businessId: "devco", minutes: 5 }],
    [{ kind: "capacity", businessId: 7, minutes: 5 }],
  ])("rejects %j", (value) => {
    expect(isChange(value)).toBe(false);
  });

  it("checks business ids, ranges, duplicates and the 20-change limit", () => {
    expect(checkChanges(base, [{ kind: "capacity", businessId: "nope", minutes: 1 }])[0]).toMatch(/unknown business/);
    expect(checkChanges(base, [{ kind: "estimate", businessId: "devco", service: "design", minutes: 0 }])[0]).toMatch(/1 to 1000000/);
    expect(checkChanges(base, [{ kind: "capacity", businessId: "devco", minutes: 0 }])).toEqual([]);
    expect(checkChanges(base, [{ kind: "capacity", businessId: "devco", minutes: 1_000_001 }])[0]).toMatch(/0 to 1000000/);
    const same: Change[] = [
      { kind: "estimate", businessId: "devco", service: "design", minutes: 5 },
      { kind: "estimate", businessId: "devco", service: "design", minutes: 6 },
    ];
    expect(checkChanges(base, same)[0]).toMatch(/same field/);
    // different services of the same business are different fields
    expect(checkChanges(base, [same[0], { ...same[1], service: "coding" } as Change])).toEqual([]);
    expect(checkChanges(base, Array.from({ length: 21 }, () => same[0]))[0]).toMatch(/At most 20/);
    expect(checkChanges(base, "x")[0]).toMatch(/not a list/);
  });
});

describe("applyChanges", () => {
  it("applies the noExchange fixture without touching the input", () => {
    const before = JSON.stringify(base);
    const next = applyChanges(base, noExchangeChanges);
    expect(JSON.stringify(base)).toBe(before);
    expect(next.businesses[1].estimates.find((e) => e.service === "design")?.minutes).toBe(600);
    expect(next.businesses[1].estimates.find((e) => e.service === "writing")?.minutes).toBe(480);
    expect(next.businesses[0]).toEqual(base.businesses[0]);
    expect(next.confirmed).toBe(true);
  });

  it("applies the all-zero capacity fixture", () => {
    expect(applyChanges(base, infeasibleChanges).businesses.map((b) => b.capacityMinutes)).toEqual([0, 0, 0]);
  });

  it("sets coordination and adds a previously unsupported service estimate", () => {
    const next = applyChanges(base, [
      { kind: "coordination", businessId: "copyco", minutes: 60 },
      { kind: "estimate", businessId: "devco", service: "writing", minutes: 90 },
    ]);
    expect(next.businesses[2].coordinationMinutes).toBe(60);
    expect(next.businesses[0].estimates).toContainEqual({ service: "writing", minutes: 90 });
  });
});

describe("sameScenario", () => {
  it("ignores estimate order but nothing else", () => {
    const reordered = structuredClone(base);
    reordered.businesses[0].estimates.reverse();
    expect(sameScenario(base, reordered)).toBe(true);
    const different = structuredClone(base);
    different.businesses[0].capacityMinutes += 1;
    expect(sameScenario(base, different)).toBe(false);
    const renamed = structuredClone(base);
    renamed.deliverables[0].ownerId = "copyco";
    expect(sameScenario(base, renamed)).toBe(false);
  });
});

describe("describeChange and changeFromDraft", () => {
  it("shows old and new values from the scenario", () => {
    expect(describeChange(base, noExchangeChanges[0])).toBe("DesignCo design estimate: 3 h → 10 h");
    expect(describeChange(base, infeasibleChanges[0])).toBe("DevCo capacity: 10 h → 0 h");
    expect(describeChange(base, { kind: "estimate", businessId: "devco", service: "writing", minutes: 90 })).toBe(
      "DevCo writing estimate: unsupported → 1.5 h",
    );
    expect(minutesLabel(10)).toBe("10 min");
  });

  it("builds changes with the same duration rules as the main form", () => {
    expect(changeFromDraft({ kind: "estimate", businessId: "designco", service: "design", value: "10" })).toEqual({
      ok: true,
      change: noExchangeChanges[0],
    });
    expect(changeFromDraft({ kind: "capacity", businessId: "devco", service: "design", value: "0" })).toEqual({
      ok: true,
      change: { kind: "capacity", businessId: "devco", minutes: 0 },
    });
    expect(changeFromDraft({ kind: "estimate", businessId: "devco", service: "design", value: "0" }).ok).toBe(false);
    expect(changeFromDraft({ kind: "capacity", businessId: "devco", service: "design", value: "0.001" }).ok).toBe(false);
    expect(changeFromDraft({ kind: "capacity", businessId: "", service: "design", value: "1" }).ok).toBe(false);
  });
});

describe("compareAssignments", () => {
  const result = (assignments: Result["assignments"]) => ({ assignments }) as unknown as Result;
  const loop = result([
    { deliverableId: "d-design", providerId: "designco", minutes: 180 },
    { deliverableId: "d-writing", providerId: "copyco", minutes: 180 },
    { deliverableId: "d-coding", providerId: "devco", minutes: 180 },
  ]);

  it("marks only what actually differs and uses null, not a guess, when nothing is assigned", () => {
    const partial = result([
      { deliverableId: "d-design", providerId: "designco", minutes: 180 },
      { deliverableId: "d-writing", providerId: "designco", minutes: 480 },
    ]);
    const rows = compareAssignments(base, loop, partial);
    expect(rows.map((r) => r.changed)).toEqual([false, true, true]);
    expect(rows[2]).toMatchObject({ beforeProvider: "devco", afterProvider: null, afterMinutes: null });
    expect(compareAssignments(base, loop, result([])).every((r) => r.changed && r.afterProvider === null)).toBe(true);
    expect(compareAssignments(base, loop, loop).some((r) => r.changed)).toBe(false);
  });
});

describe("what-if state", () => {
  const payload = (scenario: Scenario) => ({ changes: noExchangeChanges, scenario, result: fakeResult, mode: "fixture" as const });
  const optimized = () => {
    let s = scenarioReducer(initialState(), { type: "load_canonical" });
    s = scenarioReducer(s, { type: "set_result", forVersion: s.version, result: fakeResult });
    return s;
  };

  it("keeps the base result next to the what-if and numbers what-ifs", () => {
    let s = optimized();
    s = scenarioReducer(s, { type: "set_whatif", forVersion: s.version, whatIf: payload(base) });
    expect(s.whatIf).toMatchObject({ n: 1, baseVersion: s.version });
    expect(s.result).not.toBeNull();
    s = scenarioReducer(s, { type: "set_whatif", forVersion: s.version, whatIf: payload(base) });
    expect(s.whatIf?.n).toBe(2);
    expect(scenarioReducer(s, { type: "clear_whatif" })).toMatchObject({ whatIf: null, result: fakeResult });
  });

  it("refuses a what-if without a base result or for an older version", () => {
    const loaded = scenarioReducer(initialState(), { type: "load_canonical" });
    expect(scenarioReducer(loaded, { type: "set_whatif", forVersion: loaded.version, whatIf: payload(base) }).whatIf).toBeNull();
    const s = optimized();
    expect(scenarioReducer(s, { type: "set_whatif", forVersion: s.version - 1, whatIf: payload(base) }).whatIf).toBeNull();
  });

  it("every edit, load, reset and new base result clears the what-if", () => {
    let s = optimized();
    s = scenarioReducer(s, { type: "set_whatif", forVersion: s.version, whatIf: payload(base) });
    expect(scenarioReducer(s, { type: "edit_business", index: 0, patch: { name: "Z" } })).toMatchObject({ whatIf: null, whatIfCount: 0 });
    expect(scenarioReducer(s, { type: "load_canonical" }).whatIf).toBeNull();
    expect(scenarioReducer(s, { type: "reset" }).whatIf).toBeNull();
    expect(scenarioReducer(s, { type: "set_result", forVersion: s.version, result: fakeResult }).whatIf).toBeNull();
  });
});
