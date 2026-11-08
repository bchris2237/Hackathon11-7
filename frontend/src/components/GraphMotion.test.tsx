// Graph motion rules (C9). jsdom has no layout, so React Flow draws no edges here; the logic that
// decides what animates is tested directly, and the drawn result is checked in the browser spec
// (tests/e2e/graph-motion.spec.ts). Results below are hand-written TEST INPUTS, not solver output.
import { act, render, renderHook, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Business, Result, Scenario, Service } from "../../../contracts/models";
import NetworkGraph, {
  ENTER_MS,
  buildGraph,
  changedExchangeEdges,
  useEnteringEdges,
  usePrefersReducedMotion,
} from "./NetworkGraph";

const canonical: Scenario = JSON.parse(readFileSync(resolve(__dirname, "../../../fixtures/canonical.json"), "utf8"));
const cert = {
  schemaVersion: "1" as const,
  scenarioDigest: "a".repeat(64),
  allocationDigest: "b".repeat(64),
  verification: "valid" as const,
  assumptions: ["Estimates are supplied by the user."],
  checks: [{ name: "within capacity", passed: true as const }],
};

/** A result with the given providers for d-design, d-writing, d-coding (minutes 180 each). */
const result = (providers: [string, string, string], minutes = 180, certificate: Result["certificate"] = cert): Result => ({
  schemaVersion: "1",
  status: "collaboration",
  assignments: [
    { deliverableId: "d-design", providerId: providers[0], minutes },
    { deliverableId: "d-writing", providerId: providers[1], minutes },
    { deliverableId: "d-coding", providerId: providers[2], minutes },
  ],
  businessMetrics: ["devco", "designco", "copyco"].map((businessId) => ({
    businessId,
    baselineMinutes: 480,
    productionMinutes: 180,
    coordinationMinutes: 0,
    effortMinutes: 180,
    netSavingsMinutes: 300,
  })),
  totals: { baselineMinutes: 1440, productionMinutes: 540, coordinationMinutes: 0, effortMinutes: 540, grossSavingsMinutes: 900, netSavingsMinutes: 900 },
  reasons: [],
  certificate,
});
const LOOP = result(["designco", "copyco", "devco"]);
const SELF = result(["devco", "designco", "copyco"], 480);
const SWAPPED = result(["copyco", "copyco", "devco"]); // only d-design differs from LOOP (copyco instead of designco)
const infeasible: Result = { ...LOOP, status: "infeasible", assignments: [], businessMetrics: null, totals: null, certificate: null, reasons: [{ code: "X", message: "none" }] };

function mockMotionPreference(reduce: boolean) {
  const listeners = new Set<() => void>();
  const query = {
    matches: reduce,
    media: "(prefers-reduced-motion: reduce)",
    addEventListener: (_: string, fn: () => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
  };
  vi.stubGlobal("matchMedia", vi.fn(() => query));
  return {
    set(value: boolean) {
      query.matches = value;
      listeners.forEach((fn) => fn());
    },
    listeners,
  };
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("changedExchangeEdges", () => {
  it("lists only arrows that are new or different", () => {
    expect([...changedExchangeEdges(canonical, LOOP, result(["designco", "copyco", "devco"]))]).toEqual([]);
    // d-design now by copyco (a different provider); the other two arrows are untouched.
    expect([...changedExchangeEdges(canonical, LOOP, SWAPPED)]).toEqual(["edge-d-design"]);
    // same providers, different minutes
    expect([...changedExchangeEdges(canonical, LOOP, result(["designco", "copyco", "devco"], 200))].sort()).toEqual([
      "edge-d-coding",
      "edge-d-design",
      "edge-d-writing",
    ]);
  });

  it("does nothing without a previous result", () => {
    expect(changedExchangeEdges(canonical, null, LOOP).size).toBe(0);
  });

  it("ignores self-work, which is not an arrow, and arrows that disappear", () => {
    expect(changedExchangeEdges(canonical, LOOP, SELF).size).toBe(0);
  });

  it("treats every arrow as new after an infeasible result", () => {
    expect(changedExchangeEdges(canonical, infeasible, LOOP).size).toBe(3);
  });

  it("never names an arrow for an unknown deliverable", () => {
    const odd: Result = { ...LOOP, assignments: [{ deliverableId: "ghost", providerId: "devco", minutes: 1 }] };
    expect(changedExchangeEdges(canonical, LOOP, odd).size).toBe(0);
  });
});

describe("useEnteringEdges", () => {
  beforeEach(() => vi.useFakeTimers());
  const run = (before: Result | null | undefined, now: Result, verified = true, reduced = false) =>
    renderHook((p: { r: Result; verified: boolean; reduced: boolean }) => useEnteringEdges(canonical, p.r, before, p.verified, p.reduced), {
      initialProps: { r: now, verified, reduced },
    });

  it("animates nothing on the first draw of a lone result", () => {
    expect(run(undefined, LOOP).result.current.size).toBe(0);
  });

  it("animates only the differing arrows when drawn as a variant of another result, then stops", () => {
    const { result: entering } = run(LOOP, SWAPPED);
    expect([...entering.current]).toEqual(["edge-d-design"]);
    act(() => void vi.advanceTimersByTime(ENTER_MS + 200));
    expect(entering.current.size).toBe(0);
  });

  it("starts a fade when a later verified result changes an arrow, and not for an identical result", () => {
    const hook = run(undefined, LOOP);
    hook.rerender({ r: SWAPPED, verified: true, reduced: false });
    expect([...hook.result.current]).toEqual(["edge-d-design"]);
    act(() => void vi.advanceTimersByTime(ENTER_MS + 200));
    hook.rerender({ r: { ...SWAPPED }, verified: true, reduced: false }); // a new object with the same assignments
    expect(hook.result.current.size).toBe(0);
  });

  it("rapid updates: the newest result owns the animation and timers do not pile up", () => {
    const hook = run(undefined, LOOP);
    const a = result(["copyco", "copyco", "devco"]);
    const b = result(["designco", "devco", "devco"]);
    const c = result(["designco", "copyco", "copyco"]);
    for (const r of [a, b, c, LOOP, a, b, c]) {
      hook.rerender({ r, verified: true, reduced: false });
      act(() => void vi.advanceTimersByTime(20));
    }
    // b -> c: d-writing changes provider (an arrow); d-coding is self-work for its owner (no arrow); d-design is the same.
    expect([...hook.result.current]).toEqual(["edge-d-writing"]);
    expect(vi.getTimerCount()).toBe(1); // exactly one pending clear
    act(() => void vi.advanceTimersByTime(ENTER_MS + 200));
    expect(hook.result.current.size).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("does not animate an unverified result (but still tracks it as the comparison point)", () => {
    const hook = run(undefined, LOOP, false);
    hook.rerender({ r: SWAPPED, verified: false, reduced: false });
    expect(hook.result.current.size).toBe(0);
    hook.rerender({ r: LOOP, verified: true, reduced: false });
    expect([...hook.result.current]).toEqual(["edge-d-design"]); // compared with SWAPPED, the last thing shown
  });

  it("does not animate under reduced motion, even for a verified change", () => {
    const hook = run(LOOP, SWAPPED, true, true);
    expect(hook.result.current.size).toBe(0);
    hook.rerender({ r: LOOP, verified: true, reduced: true });
    expect(hook.result.current.size).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("clears its timer on unmount", () => {
    const hook = run(LOOP, SWAPPED);
    expect(vi.getTimerCount()).toBe(1);
    hook.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe("usePrefersReducedMotion", () => {
  it("reads the system setting and follows a change while the page is open", () => {
    const motion = mockMotionPreference(false);
    const { result: reduced, unmount } = renderHook(() => usePrefersReducedMotion());
    expect(reduced.current).toBe(false);
    act(() => motion.set(true));
    expect(reduced.current).toBe(true);
    act(() => motion.set(false));
    expect(reduced.current).toBe(false);
    unmount();
    expect(motion.listeners.size).toBe(0); // listener removed
  });

  it("is false when the browser has no matchMedia", () => {
    vi.stubGlobal("matchMedia", undefined);
    const { result: reduced } = renderHook(() => usePrefersReducedMotion());
    expect(reduced.current).toBe(false);
  });
});

describe("NetworkGraph", () => {
  it("offers real zoom buttons so the keyboard can control the view", async () => {
    const user = userEvent.setup();
    render(<NetworkGraph scenario={canonical} result={LOOP} />);
    const zoomIn = screen.getByRole("button", { name: /zoom in/i });
    expect(screen.getByRole("button", { name: /zoom out/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /fit view/i })).toBeInTheDocument();
    await user.tab();
    expect(document.activeElement?.tagName).toBe("BUTTON");
    zoomIn.focus();
    await user.keyboard("{Enter}");
    expect(zoomIn).toHaveFocus(); // still there, nothing re-mounted
    // The picture itself adds no tab stops.
    expect(document.querySelectorAll("[data-testid=network-graph] [tabindex='0']")).toHaveLength(0);
  });

  it("keeps the text list in step with each result immediately, whatever the motion setting", () => {
    for (const reduce of [false, true]) {
      mockMotionPreference(reduce);
      const { rerender, unmount } = render(<NetworkGraph scenario={canonical} result={LOOP} />);
      expect(screen.getByTestId("assignment-list").querySelectorAll("li[data-kind='exchange']")).toHaveLength(3);
      rerender(<NetworkGraph scenario={canonical} result={SELF} />);
      expect(screen.getByTestId("assignment-list").querySelectorAll("li[data-kind='exchange']")).toHaveLength(0);
      expect(screen.getByTestId("no-exchange-note")).toBeInTheDocument();
      rerender(<NetworkGraph scenario={canonical} result={SWAPPED} />);
      expect(screen.getByTestId("assignment-list")).toHaveTextContent("CopyCo does d-design");
      unmount();
    }
  });

  it("an inconsistent result is never animated or drawn", () => {
    const bad: Result = { ...LOOP, assignments: [{ deliverableId: "d-design", providerId: "ghost", minutes: 1 }] };
    render(<NetworkGraph scenario={canonical} result={bad} previousResult={LOOP} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/no graph is drawn/);
    expect(screen.queryByTestId("network-graph")).not.toBeInTheDocument();
  });

  it("node positions do not depend on the result, so nothing moves when results change", () => {
    const positions = (r: Result) => Object.fromEntries(buildGraph(canonical, r).nodes.map((n) => [n.id, n.position]));
    const base = positions(LOOP);
    for (const r of [SELF, SWAPPED, infeasible, result(["copyco", "devco", "designco"], 999)]) expect(positions(r)).toEqual(base);
  });

  it("many rapid result updates and an unmount leave no timers and no console errors", () => {
    vi.useFakeTimers();
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const { rerender, unmount } = render(<NetworkGraph scenario={canonical} result={LOOP} />);
    const variants = [SWAPPED, LOOP, SELF, SWAPPED, result(["designco", "devco", "devco"]), LOOP];
    for (let i = 0; i < 60; i++) rerender(<NetworkGraph scenario={canonical} result={variants[i % variants.length]} />);
    unmount();
    act(() => void vi.runOnlyPendingTimers());
    expect(vi.getTimerCount()).toBe(0);
    expect(error).not.toHaveBeenCalled();
  });
});

// ---- bounded five-company fixture ------------------------------------------------------------
// The v1 MVP is exactly three businesses (docs/contracts.md); five is the optional extension and is
// NOT claimed to exist in any deployed backend. This only exercises the graph component on data of
// that size to see how it behaves.

function fiveCompanies(): { scenario: Scenario; variants: Result[] } {
  const ids = ["alpha", "bravo", "charlie", "delta", "echo"];
  const services: Service[] = ["coding", "design", "writing"];
  const businesses = ids.map(
    (id, i): Business => ({
      id,
      name: id[0].toUpperCase() + id.slice(1),
      capacityMinutes: 600,
      coordinationMinutes: 0,
      estimates: services.map((service, s) => ({ service, minutes: 150 + 30 * ((i + s) % 4) })),
    }),
  );
  const owners = ["alpha", "bravo", "charlie", "delta", "echo", "alpha"];
  const deliverables = owners.map((ownerId, i) => ({ id: `d-${i + 1}`, ownerId, service: services[i % 3] }));
  const scenario = { schemaVersion: "1", businesses, deliverables, confirmed: true } as unknown as Scenario;
  const variant = (shift: number): Result => ({
    schemaVersion: "1",
    status: "collaboration",
    assignments: deliverables.map((d, i) => ({ deliverableId: d.id, providerId: ids[(i + 1 + shift) % 5], minutes: 150 + 30 * ((i + shift) % 4) })),
    businessMetrics: ids.map((businessId) => ({ businessId, baselineMinutes: 300, productionMinutes: 200, coordinationMinutes: 0, effortMinutes: 200, netSavingsMinutes: 100 })),
    totals: { baselineMinutes: 1500, productionMinutes: 1000, coordinationMinutes: 0, effortMinutes: 1000, grossSavingsMinutes: 500, netSavingsMinutes: 500 },
    reasons: [],
    certificate: cert,
  });
  return { scenario, variants: [0, 1, 2, 3].map(variant) };
}

describe("bounded five-company fixture (graph component only)", () => {
  it("builds distinct positions, unique edge ids, and a list entry per deliverable", () => {
    const { scenario, variants } = fiveCompanies();
    for (const r of variants) {
      const graph = buildGraph(scenario, r);
      expect(graph.problems).toEqual([]);
      expect(graph.nodes).toHaveLength(5);
      expect(new Set(graph.nodes.map((n) => `${Math.round(n.position.x)},${Math.round(n.position.y)}`)).size).toBe(5);
      expect(new Set(graph.edges.map((e) => e.id)).size).toBe(graph.edges.length);
      expect(graph.lines).toHaveLength(6);
    }
    // With six deliverables over five businesses some pairs have two arrows; those bow apart.
    const edges = buildGraph(scenario, variants[0]).edges;
    const bowed = edges.filter((e) => (e.data as { offset: number }).offset > 0);
    expect(bowed.length === 0 || bowed.every((e) => (e.data as { offset: number }).offset >= 34)).toBe(true);
  });

  it("compares update cost for three and five companies (jsdom render cost, not browser paint)", () => {
    const three = [LOOP, SWAPPED, SELF, result(["designco", "devco", "devco"])];
    const five = fiveCompanies();
    const time = (scenario: Scenario, variants: Result[]) => {
      const { rerender, unmount } = render(<NetworkGraph scenario={scenario} result={variants[0]} />);
      const rounds = 60;
      const start = performance.now();
      for (let i = 0; i < rounds; i++) rerender(<NetworkGraph scenario={scenario} result={variants[i % variants.length]} />);
      const perUpdate = (performance.now() - start) / rounds;
      unmount();
      return perUpdate;
    };
    const t3 = time(canonical, three);
    const t5 = time(five.scenario, five.variants);
    console.info(`[C9-PERF] ms per result update in jsdom: 3 companies ${t3.toFixed(1)}, 5 companies ${t5.toFixed(1)}`);
    // A generous bound: it must stay interactive, not a performance promise.
    expect(t3).toBeLessThan(250);
    expect(t5).toBeLessThan(250);
  });
});
