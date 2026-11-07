// @vitest-environment node
// TypeScript half of the contract parity check. It runs the same fixtures and the same
// invalid cases (fixtures/contract-cases.json) as tests/backend/test_contracts_parity.py,
// using Ajv against the canonical JSON Schemas.
import Ajv2020 from "ajv/dist/2020.js";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import type { ExtractResponse, HealthResponse, Scenario } from "../../contracts/models";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const readJson = (rel: string): any => JSON.parse(readFileSync(resolve(root, rel), "utf8"));

const BASE = "https://trademind.example/contracts/";
const ajv = new Ajv2020({ strict: true, strictTypes: false, allErrors: true });
for (const file of ["scenario", "result", "api"]) {
  ajv.addSchema(readJson(`contracts/${file}.schema.json`));
}
const validate = (file: string, def?: string) => {
  const fn = ajv.getSchema(BASE + file + (def ? `#/$defs/${def}` : ""));
  if (!fn) throw new Error(`schema not found: ${file} ${def ?? ""}`);
  return fn;
};

const cases = readJson("fixtures/contract-cases.json");
const canonical = readJson("fixtures/canonical.json");

function applyOps(document: any, ops: any[]) {
  const doc = structuredClone(document);
  for (const op of ops) {
    const parents: (string | number)[] = op.path.slice(0, -1);
    const last: string | number = op.path[op.path.length - 1];
    const node = parents.reduce((n: any, key) => n[key], doc);
    if (op.op === "set") node[last] = op.value;
    else if (op.op === "delete") Array.isArray(node) ? node.splice(last as number, 1) : delete node[last];
    else if (op.op === "append") node[last].push(op.value);
    else throw new Error(`unknown op ${op.op}`);
  }
  return doc;
}

const replaceCanonical = (node: any): any =>
  node === "$canonical"
    ? structuredClone(canonical)
    : Array.isArray(node)
      ? node.map(replaceCanonical)
      : node && typeof node === "object"
        ? Object.fromEntries(Object.entries(node).map(([k, v]) => [k, replaceCanonical(v)]))
        : node;

describe("schemas compile", () => {
  it("compiles scenario, result and every API definition", () => {
    expect(validate("scenario.schema.json")).toBeTypeOf("function");
    expect(validate("result.schema.json")).toBeTypeOf("function");
    for (const def of Object.keys(readJson("contracts/api.schema.json").$defs)) {
      expect(validate("api.schema.json", def), def).toBeTypeOf("function");
    }
  });
});

describe("valid fixtures", () => {
  it.each(["canonical", "coordination"])("%s.json is a valid Scenario", (name) => {
    const data: Scenario = readJson(`fixtures/${name}.json`);
    const check = validate("scenario.schema.json");
    expect(check(data), JSON.stringify(check.errors)).toBe(true);
  });

  it("extraction.json request and response are valid and labelled as fixture", () => {
    const data = readJson("fixtures/extraction.json");
    expect(validate("api.schema.json", "ExtractRequest")(data.request)).toBe(true);
    const response: ExtractResponse = data.response;
    expect(validate("api.schema.json", "ExtractResponse")(response)).toBe(true);
    expect(response.mode).toBe("fixture");
  });

  it.each(["noExchange", "infeasible"])("disruption.json %s changes are valid Change objects", (key) => {
    const changes = readJson("fixtures/disruption.json")[key].changes;
    expect(changes.length).toBeGreaterThan(0);
    for (const change of changes) expect(validate("api.schema.json", "Change")(change)).toBe(true);
  });

  it("a health body typed from models.ts validates", () => {
    const health: HealthResponse = { status: "ok", schemaVersion: "1", commit: "abc", mode: "fixture" };
    expect(validate("api.schema.json", "HealthResponse")(health)).toBe(true);
  });
});

describe("invalid inputs are rejected (same cases as the Python suite)", () => {
  it.each<any>(cases.scenarioInvalid)("scenario: $name", (c) => {
    expect(validate("scenario.schema.json")(applyOps(canonical, c.ops))).toBe(false);
  });

  it.each<any>(cases.simulateInvalid)("simulate: $name", (c) => {
    expect(validate("api.schema.json", "SimulateRequest")(replaceCanonical(c.request))).toBe(false);
  });

  it.each<any>(cases.simulateValid)("simulate valid: $name", (c) => {
    const check = validate("api.schema.json", "SimulateRequest");
    const request = replaceCanonical(c.request);
    expect(check(request), JSON.stringify(check.errors)).toBe(true);
  });
});

describe("result shape rules", () => {
  const base = { schemaVersion: "1", assignments: [], businessMetrics: null, totals: null, reasons: [], certificate: null };
  it("accepts infeasible with null totals and certificate", () => {
    expect(validate("result.schema.json")({ ...base, status: "infeasible" })).toBe(true);
  });
  it("rejects collaboration with null totals (no fabricated or missing numbers)", () => {
    expect(validate("result.schema.json")({ ...base, status: "collaboration" })).toBe(false);
  });
});
