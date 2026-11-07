import { describe, expect, it } from "vitest";
import canonical from "../../../fixtures/canonical.json";
import type { Result } from "../../../contracts/models";
import {
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
