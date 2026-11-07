// All Results below are hand-written TEST INPUTS shaped like the contract. They are not
// solver output and the app never contains them; they stand in for a stubbed backend.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Result, Scenario } from "../../../contracts/models";
import App from "../App";
import { isResult } from "../lib/api";
import NetworkGraph, { buildGraph } from "./NetworkGraph";

const root = resolve(__dirname, "../../..");
const canonical: Scenario = JSON.parse(readFileSync(resolve(root, "fixtures/canonical.json"), "utf8"));

const metrics = (ids: string[], production: number, effort: number, baseline: number) =>
  ids.map((businessId) => ({
    businessId,
    baselineMinutes: baseline,
    productionMinutes: production,
    coordinationMinutes: effort - production,
    effortMinutes: effort,
    netSavingsMinutes: baseline - effort,
  }));
const IDS = ["devco", "designco", "copyco"];

const collaboration: Result = {
  schemaVersion: "1",
  status: "collaboration",
  assignments: [
    { deliverableId: "d-design", providerId: "designco", minutes: 180 },
    { deliverableId: "d-writing", providerId: "copyco", minutes: 180 },
    { deliverableId: "d-coding", providerId: "devco", minutes: 180 },
  ],
  businessMetrics: metrics(IDS, 180, 180, 480),
  totals: {
    baselineMinutes: 1440,
    productionMinutes: 540,
    coordinationMinutes: 0,
    effortMinutes: 540,
    grossSavingsMinutes: 900,
    netSavingsMinutes: 900,
  },
  reasons: [],
  certificate: null,
};

const noExchange: Result = {
  schemaVersion: "1",
  status: "no_exchange",
  assignments: [
    { deliverableId: "d-design", providerId: "devco", minutes: 480 },
    { deliverableId: "d-writing", providerId: "designco", minutes: 480 },
    { deliverableId: "d-coding", providerId: "copyco", minutes: 480 },
  ],
  businessMetrics: metrics(IDS, 480, 480, 480),
  totals: {
    baselineMinutes: 1440,
    productionMinutes: 1440,
    coordinationMinutes: 0,
    effortMinutes: 1440,
    grossSavingsMinutes: 0,
    netSavingsMinutes: 0,
  },
  reasons: [{ code: "NO_BENEFICIAL_EXCHANGE", message: "No exchange improves on self-production." }],
  certificate: null,
};

const infeasible: Result = {
  schemaVersion: "1",
  status: "infeasible",
  assignments: [],
  businessMetrics: null,
  totals: null,
  reasons: [{ code: "CAPACITY_EXCEEDED", message: "No allocation fits every capacity." }],
  certificate: null,
};

describe("buildGraph", () => {
  it("canonical loop: exactly three edges, provider -> owner, ids from deliverables", () => {
    const g = buildGraph(canonical, collaboration);
    expect(g.problems).toEqual([]);
    expect(g.nodes.map((n) => n.id).sort()).toEqual(["copyco", "designco", "devco"]);
    expect(g.edges).toHaveLength(3);
    const byId = Object.fromEntries(g.edges.map((e) => [e.id, e]));
    expect([byId["edge-d-design"].source, byId["edge-d-design"].target]).toEqual(["designco", "devco"]);
    expect([byId["edge-d-writing"].source, byId["edge-d-writing"].target]).toEqual(["copyco", "designco"]);
    expect([byId["edge-d-coding"].source, byId["edge-d-coding"].target]).toEqual(["devco", "copyco"]);
    expect(byId["edge-d-design"].label).toBe("d-design · design · 180 min (3 h)");
  });

  it("no_exchange draws no exchange loop; self-work sits on the nodes", () => {
    const g = buildGraph(canonical, noExchange);
    expect(g.edges).toEqual([]);
    expect(g.lines.every((l) => l.self)).toBe(true);
    const devco = g.nodes.find((n) => n.id === "devco")!;
    expect((devco.data as { label: string }).label).toContain("Self: d-design · design · 480 min (8 h)");
  });

  it("infeasible has nothing to draw", () => {
    const g = buildGraph(canonical, infeasible);
    expect(g.edges).toEqual([]);
    expect(g.lines).toEqual([]);
  });

  it("parallel exchanges with identical service and time keep unique edge ids", () => {
    const scenario: Scenario = structuredClone(canonical);
    scenario.deliverables = [
      { id: "d-a", ownerId: "devco", service: "design" },
      { id: "d-b", ownerId: "devco", service: "design" },
    ];
    const result: Result = {
      ...collaboration,
      assignments: [
        { deliverableId: "d-a", providerId: "designco", minutes: 180 },
        { deliverableId: "d-b", providerId: "designco", minutes: 180 },
      ],
    };
    const g = buildGraph(scenario, result);
    expect(g.edges).toHaveLength(2);
    expect(new Set(g.edges.map((e) => e.id)).size).toBe(2);
  });

  it("refuses to draw ids the scenario does not contain", () => {
    const bad: Result = { ...collaboration, assignments: [{ deliverableId: "d-design", providerId: "ghost", minutes: 1 }] };
    expect(buildGraph(canonical, bad).problems[0]).toMatch(/unknown provider "ghost"/);
    const bad2: Result = { ...collaboration, assignments: [{ deliverableId: "nope", providerId: "devco", minutes: 1 }] };
    expect(buildGraph(canonical, bad2).problems[0]).toMatch(/unknown deliverable/);
  });
});

describe("NetworkGraph component", () => {
  it("renders an accessible text list that mirrors the graph", () => {
    render(<NetworkGraph scenario={canonical} result={collaboration} />);
    const items = within(screen.getByTestId("assignment-list")).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent("DesignCo does d-design (design, 180 min (3 h)) for DevCo.");
    expect(screen.getByRole("img")).toHaveAccessibleName(/3 exchange arrows/);
    expect(screen.queryByTestId("no-exchange-note")).not.toBeInTheDocument();
  });

  it("says plainly that no exchange exists for no_exchange", () => {
    render(<NetworkGraph scenario={canonical} result={noExchange} />);
    expect(screen.getByTestId("no-exchange-note")).toHaveTextContent(/No exchange arrows/);
    expect(within(screen.getByTestId("assignment-list")).getAllByRole("listitem")[0]).toHaveTextContent(/its own/);
  });

  it("shows an alert instead of a graph for an inconsistent result", () => {
    const bad: Result = { ...collaboration, assignments: [{ deliverableId: "d-design", providerId: "ghost", minutes: 1 }] };
    render(<NetworkGraph scenario={canonical} result={bad} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/no graph is drawn/);
    expect(screen.queryByTestId("network-graph")).not.toBeInTheDocument();
  });
});

describe("isResult", () => {
  it("accepts the three statuses and rejects missing numbers outside infeasible", () => {
    expect(isResult(collaboration)).toBe(true);
    expect(isResult(noExchange)).toBe(true);
    expect(isResult(infeasible)).toBe(true);
    expect(isResult({ ...collaboration, totals: null })).toBe(false);
    expect(isResult({ ...infeasible, assignments: collaboration.assignments })).toBe(false);
    expect(isResult({ ...collaboration, status: "weird" })).toBe(false);
  });
});

// ---- full flow through the screen with a stubbed backend --------------------------------

const HEALTH = { status: "ok", schemaVersion: "1", commit: "abc", mode: "fixture" };
function stubBackend(optimize: (body: any) => Response | Promise<Response>) {
  const calls: { path: string; body: any }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const path = new URL(url).pathname;
      if (path === "/health") return Response.json(HEALTH);
      const body = JSON.parse(init.body as string);
      calls.push({ path, body });
      if (path === "/confirm") return Response.json({ scenario: body.scenario });
      if (path === "/optimize") return optimize(body);
      throw new TypeError("no route " + path);
    }),
  );
  return calls;
}
afterEach(() => vi.unstubAllGlobals());

async function confirmedApp() {
  const user = userEvent.setup();
  render(<App />);
  await user.click(screen.getByRole("button", { name: "Load fictional sample" }));
  expect(screen.getByRole("button", { name: "Optimize" })).toBeDisabled(); // not confirmed yet
  await user.click(screen.getByRole("button", { name: "Confirm scenario" }));
  await screen.findByTestId("confirmed-note");
  return user;
}

describe("optimize through the screen", () => {
  it("optimizes the confirmed canonical scenario and shows the loop, table and savings", async () => {
    const calls = stubBackend(() => Response.json({ result: collaboration, mode: "fixture" }));
    const user = await confirmedApp();
    await user.click(screen.getByRole("button", { name: "Optimize" }));
    expect(await screen.findByTestId("result-status")).toHaveTextContent("Beneficial exchange found");

    const sent = calls.find((c) => c.path === "/optimize")!;
    expect(sent.body.scenario.confirmed).toBe(true);
    expect(sent.body.scenario.businesses).toHaveLength(3);
    expect(screen.getByTestId("savings-summary")).toHaveTextContent("900 min (15 h), 62.5% of baseline");
    expect(screen.getByTestId("metrics")).toHaveTextContent("1440");
    expect(within(screen.getByTestId("assignment-list")).getAllByRole("listitem")).toHaveLength(3);
    expect(screen.getByTestId("result-slot")).toHaveTextContent(/available/);
  });

  it("shows infeasible reasons and no numbers or graph", async () => {
    stubBackend(() => Response.json({ result: infeasible, mode: "fixture" }));
    const user = await confirmedApp();
    await user.click(screen.getByRole("button", { name: "Optimize" }));
    expect(await screen.findByTestId("result-status")).toHaveTextContent("Infeasible");
    expect(screen.getByTestId("reasons")).toHaveTextContent("CAPACITY_EXCEEDED");
    expect(screen.queryByTestId("metrics")).not.toBeInTheDocument();
    expect(screen.queryByTestId("network-graph")).not.toBeInTheDocument();
  });

  it("an edit clears the result and the confirmation, and Optimize is disabled again", async () => {
    stubBackend(() => Response.json({ result: collaboration, mode: "fixture" }));
    const user = await confirmedApp();
    await user.click(screen.getByRole("button", { name: "Optimize" }));
    await screen.findByTestId("optimize-result");
    await user.type(screen.getByLabelText("Business 1 name"), "x");
    expect(screen.queryByTestId("optimize-result")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Optimize" })).toBeDisabled();
  });

  it("drops an out-of-order reply that arrives after an edit", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    stubBackend(async () => {
      await gate;
      return Response.json({ result: collaboration, mode: "fixture" });
    });
    const user = await confirmedApp();
    await user.click(screen.getByRole("button", { name: "Optimize" }));
    await screen.findByText("Optimizing…");
    await user.type(screen.getByLabelText("Business 1 name"), "x");
    release();
    await new Promise((r) => setTimeout(r, 30));
    expect(screen.queryByTestId("optimize-result")).not.toBeInTheDocument();
    expect(screen.getByTestId("result-slot")).toHaveTextContent(/No result/);
  });

  it("shows a server failure with no substituted result", async () => {
    stubBackend(() =>
      Response.json({ error: { code: "UNAVAILABLE", message: "Solver down.", fields: [], requestId: "r-9" } }, { status: 503 }),
    );
    const user = await confirmedApp();
    await user.click(screen.getByRole("button", { name: "Optimize" }));
    expect(await screen.findByText(/Optimization failed/)).toHaveTextContent(/UNAVAILABLE.*r-9/);
    expect(screen.queryByTestId("optimize-result")).not.toBeInTheDocument();
  });

  it("rejects a result in the wrong mode and a malformed result", async () => {
    stubBackend(() => Response.json({ result: collaboration, mode: "live" }));
    const user = await confirmedApp();
    await user.click(screen.getByRole("button", { name: "Optimize" }));
    expect(await screen.findByText(/Result discarded/)).toBeInTheDocument();
    expect(screen.queryByTestId("optimize-result")).not.toBeInTheDocument();
  });

  it("rejects collaboration with null totals", async () => {
    stubBackend(() => Response.json({ result: { ...collaboration, totals: null }, mode: "fixture" }));
    const user = await confirmedApp();
    await user.click(screen.getByRole("button", { name: "Optimize" }));
    expect(await screen.findByText(/does not match the v1 contract/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId("optimize-result")).not.toBeInTheDocument());
  });
});
