// Browser checks for the restrained graph motion (C9) in real Chromium.
// The API is STUBBED with page.route; every response is a hand-written test input. The
// five-company case works by having the stubbed server answer /confirm with a five-business
// scenario (the page shows what the server returned). That exercises the graph on data of that
// size only. It says nothing about whether any backend supports five businesses.
import { expect, test, type Page } from "@playwright/test";

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "content-type",
  "access-control-allow-methods": "GET, POST, OPTIONS",
};
const cert = {
  schemaVersion: "1",
  scenarioDigest: "a1b2c3d4".repeat(8),
  allocationDigest: "0f9e8d7c".repeat(8),
  verification: "valid",
  assumptions: ["Estimates are supplied by the user and treated as exact."],
  checks: [{ name: "every business within capacity", passed: true }],
};

function resultFor(ids: string[], assignments: { deliverableId: string; providerId: string; minutes: number }[]) {
  return {
    schemaVersion: "1",
    status: "collaboration",
    assignments,
    businessMetrics: ids.map((businessId) => ({
      businessId,
      baselineMinutes: 480,
      productionMinutes: 180,
      coordinationMinutes: 0,
      effortMinutes: 180,
      netSavingsMinutes: 300,
    })),
    totals: {
      baselineMinutes: 480 * ids.length,
      productionMinutes: 180 * ids.length,
      coordinationMinutes: 0,
      effortMinutes: 180 * ids.length,
      grossSavingsMinutes: 300 * ids.length,
      netSavingsMinutes: 300 * ids.length,
    },
    reasons: [],
    certificate: cert,
  };
}

const THREE = ["devco", "designco", "copyco"];
const LOOP = resultFor(THREE, [
  { deliverableId: "d-design", providerId: "designco", minutes: 180 },
  { deliverableId: "d-writing", providerId: "copyco", minutes: 180 },
  { deliverableId: "d-coding", providerId: "devco", minutes: 180 },
]);
// Only d-design differs: copyco instead of designco.
const CHANGED = resultFor(THREE, [
  { deliverableId: "d-design", providerId: "copyco", minutes: 180 },
  { deliverableId: "d-writing", providerId: "copyco", minutes: 180 },
  { deliverableId: "d-coding", providerId: "devco", minutes: 180 },
]);

const FIVE = ["alpha", "bravo", "charlie", "delta", "echo"];
const SERVICES = ["coding", "design", "writing"];
const scenarioFive = {
  schemaVersion: "1",
  businesses: FIVE.map((id, i) => ({
    id,
    name: id[0].toUpperCase() + id.slice(1),
    capacityMinutes: 600,
    coordinationMinutes: 0,
    estimates: SERVICES.map((service, s) => ({ service, minutes: 150 + 30 * ((i + s) % 4) })),
  })),
  deliverables: ["alpha", "bravo", "charlie", "delta", "echo", "alpha"].map((ownerId, i) => ({ id: `d-${i + 1}`, ownerId, service: SERVICES[i % 3] })),
  confirmed: true,
};
const fiveResult = (shift: number) =>
  resultFor(
    FIVE,
    scenarioFive.deliverables.map((d, i) => ({ deliverableId: d.id, providerId: FIVE[(i + 1 + shift) % 5], minutes: 150 + 30 * ((i + shift) % 4) })),
  );

interface Stub {
  /** What POST /optimize answers on the 1st, 2nd, ... call (the last one repeats). */
  optimize: unknown[];
  /** What POST /simulate (apply=true) answers; its scenario is the request's scenario unless given. */
  simulate?: unknown;
  confirmScenario?: unknown;
}

async function stub(page: Page, options: Stub) {
  let optimizeCalls = 0;
  await page.route("http://127.0.0.1:8000/**", async (route) => {
    const request = route.request();
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const path = new URL(request.url()).pathname;
    const send = (body: unknown, status = 200) => route.fulfill({ status, headers: CORS, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/health") return send({ status: "ok", schemaVersion: "1", commit: "e2e-stub", mode: "fixture" });
    const body = request.postDataJSON();
    if (path === "/confirm") return send({ scenario: options.confirmScenario ?? body.scenario });
    if (path === "/optimize") {
      const result = options.optimize[Math.min(optimizeCalls, options.optimize.length - 1)];
      optimizeCalls += 1;
      return send({ result, mode: "fixture" });
    }
    if (path === "/simulate" && body.apply) {
      const scenario = structuredClone(body.scenario);
      scenario.businesses[1].estimates.find((e: any) => e.service === "design").minutes = 600;
      return send({ proposedChanges: body.changes, requiresConfirmation: false, scenario, result: options.simulate, mode: "fixture" });
    }
    return send({}, 404);
  });
}

async function toOptimized(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Load fictional sample" }).click();
  await page.getByRole("button", { name: "Confirm scenario" }).click();
  await expect(page.getByTestId("confirmed-note")).toBeVisible();
  await page.getByRole("button", { name: "Optimize" }).click();
  await expect(page.getByTestId("optimize-result")).toBeVisible();
}

const graphOf = (page: Page, scope: string) => page.getByTestId(scope).getByTestId("network-graph");
const viewportTransform = (page: Page, scope: string) => graphOf(page, scope).locator(".react-flow__viewport").evaluate((el) => (el as HTMLElement).style.transform);
const nodeTransforms = (page: Page, scope: string) =>
  graphOf(page, scope)
    .locator(".react-flow__node")
    .evaluateAll((els) => Object.fromEntries(els.map((el) => [el.getAttribute("data-id"), (el as HTMLElement).style.transform])));
const edge = (page: Page, scope: string, id: string) => graphOf(page, scope).locator(`.react-flow__edge[data-id="${id}"]`);

test.describe("a changed arrow fades in, nothing else moves", () => {
  test("what-if beside the original: only the differing arrow animates, positions match the original", async ({ page }) => {
    await stub(page, { optimize: [LOOP], simulate: CHANGED });
    await toOptimized(page);
    await expect(graphOf(page, "optimize-result").locator(".react-flow__edge")).toHaveCount(3);

    await page.getByLabel("What-if business").selectOption("designco");
    await page.getByLabel("What-if new value (hours)").fill("10");
    await page.getByRole("button", { name: "Add change" }).click();
    await page.getByRole("button", { name: "Apply these changes" }).click();
    await expect(page.getByTestId("whatif-result")).toBeVisible();
    await expect(graphOf(page, "whatif-result").locator(".react-flow__edge")).toHaveCount(3);

    // Right after drawing: the changed arrow carries the fade, the two unchanged ones do not.
    const changed = edge(page, "whatif-result", "edge-d-design");
    await expect(changed).toHaveClass(/tm-edge-enter/);
    expect(await changed.evaluate((el) => getComputedStyle(el).animationName)).toBe("tm-fade-in");
    await expect(edge(page, "whatif-result", "edge-d-writing")).not.toHaveClass(/tm-edge-enter/);
    await expect(edge(page, "whatif-result", "edge-d-coding")).not.toHaveClass(/tm-edge-enter/);
    // Its label fades too; labels of other arrows do not.
    await expect(graphOf(page, "whatif-result").locator(".tm-label-enter")).toHaveCount(1);

    // The fade never hides the things the user needs: text list, numbers, certificate, mode banner.
    const whatIf = page.getByTestId("whatif-result");
    await expect(whatIf.getByTestId("assignment-list")).toContainText("CopyCo does d-design");
    await expect(whatIf.getByTestId("metrics")).toBeVisible();
    await expect(whatIf.getByTestId("certificate-status")).toBeVisible();
    for (const id of ["mode-banner"]) {
      expect(await page.getByTestId(id).evaluate((el) => getComputedStyle(el).opacity)).toBe("1");
    }
    expect(await whatIf.getByTestId("certificate").evaluate((el) => getComputedStyle(el).opacity)).toBe("1");

    // Nodes sit where they sit in the original picture.
    expect(await nodeTransforms(page, "whatif-result")).toEqual(await nodeTransforms(page, "optimize-result"));

    // And the fade ends by itself.
    await expect(changed).not.toHaveClass(/tm-edge-enter/, { timeout: 3000 });
    await expect(graphOf(page, "whatif-result").locator(".tm-label-enter")).toHaveCount(0);
  });

  test("the original graph animates a verified change on re-optimize, keeps node positions and the user's zoom", async ({ page }) => {
    await stub(page, { optimize: [LOOP, CHANGED] });
    await toOptimized(page);
    const graph = graphOf(page, "optimize-result");
    await expect(graph.locator(".react-flow__edge")).toHaveCount(3);
    // First draw of a lone result: nothing animates.
    await expect(graph.locator(".tm-edge-enter")).toHaveCount(0);

    // The user zooms in with the keyboard.
    await graph.getByRole("button", { name: /zoom in/i }).focus();
    await page.keyboard.press("Enter");
    await expect.poll(() => viewportTransform(page, "optimize-result")).toMatch(/scale\((?!1\))/);
    const zoomed = await viewportTransform(page, "optimize-result");
    const nodesBefore = await nodeTransforms(page, "optimize-result");

    // A second, different verified result arrives.
    await page.getByRole("button", { name: "Optimize" }).click();
    await expect(page.getByTestId("assignment-list").first()).toContainText("CopyCo does d-design");
    await expect(edge(page, "optimize-result", "edge-d-design")).toHaveClass(/tm-edge-enter/);
    await expect(edge(page, "optimize-result", "edge-d-coding")).not.toHaveClass(/tm-edge-enter/);

    expect(await viewportTransform(page, "optimize-result")).toBe(zoomed); // the user's zoom is kept
    expect(await nodeTransforms(page, "optimize-result")).toEqual(nodesBefore); // nothing moved
    await expect(edge(page, "optimize-result", "edge-d-design")).not.toHaveClass(/tm-edge-enter/, { timeout: 3000 });
  });

  test("an identical re-run animates nothing", async ({ page }) => {
    await stub(page, { optimize: [LOOP, LOOP] });
    await toOptimized(page);
    await page.getByRole("button", { name: "Optimize" }).click();
    await expect(page.getByTestId("optimize-announce")).toHaveText(/Optimization finished/);
    await expect(graphOf(page, "optimize-result").locator(".tm-edge-enter")).toHaveCount(0);
  });
});

test.describe("reduced motion", () => {
  test("no arrow fades and no animation is applied, but the new arrow and list are shown at once", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await stub(page, { optimize: [LOOP], simulate: CHANGED });
    await toOptimized(page);
    await page.getByLabel("What-if business").selectOption("designco");
    await page.getByLabel("What-if new value (hours)").fill("10");
    await page.getByRole("button", { name: "Add change" }).click();
    await page.getByRole("button", { name: "Apply these changes" }).click();
    await expect(page.getByTestId("whatif-result")).toBeVisible();
    const graph = graphOf(page, "whatif-result");
    await expect(graph.locator(".react-flow__edge")).toHaveCount(3);
    await expect(graph.locator(".tm-edge-enter, .tm-label-enter")).toHaveCount(0);
    for (const el of await graph.locator(".react-flow__edge").all()) {
      expect(await el.evaluate((e) => getComputedStyle(e).animationName)).toBe("none");
      expect(await el.evaluate((e) => getComputedStyle(e).opacity)).toBe("1");
    }
    await expect(page.getByTestId("whatif-result").getByTestId("assignment-list")).toContainText("CopyCo does d-design");
  });
});

test.describe("keyboard control of the view", () => {
  test("zoom in, zoom out and fit view work from the keyboard, in that tab order, with a visible ring", async ({ page }) => {
    await stub(page, { optimize: [LOOP] });
    await toOptimized(page);
    const graph = graphOf(page, "optimize-result");
    const initial = await viewportTransform(page, "optimize-result");

    await page.getByRole("button", { name: "Optimize" }).focus();
    const seen: string[] = [];
    for (let i = 0; i < 6 && seen.length < 3; i++) {
      await page.keyboard.press("Tab");
      const info = await page.evaluate(() => {
        const el = document.activeElement as HTMLElement;
        const s = getComputedStyle(el);
        return { label: el.getAttribute("aria-label") ?? "", ring: s.outlineStyle !== "none" && parseFloat(s.outlineWidth) >= 2, inGraph: !!el.closest("[data-testid=network-graph]") };
      });
      if (info.inGraph && /zoom|fit/i.test(info.label)) {
        expect(info.ring, `no focus ring on ${info.label}`).toBe(true);
        seen.push(info.label.toLowerCase());
        if (/zoom in/.test(info.label.toLowerCase())) {
          await page.keyboard.press("Enter");
          await page.keyboard.press("Space");
        }
      }
    }
    expect(seen).toEqual(["zoom in", "zoom out", "fit view"]);
    // Enter and Space on "zoom in" changed the view; Tab to the end did not undo it by itself.
    await expect.poll(() => viewportTransform(page, "optimize-result")).not.toBe(initial);

    await graph.getByRole("button", { name: /fit view/i }).focus();
    await page.keyboard.press("Enter");
    await expect.poll(() => viewportTransform(page, "optimize-result")).toBe(initial);
  });
});

test.describe("bounded five-company data in the browser (stubbed server, graph only)", () => {
  test("draws five boxes and its arrows, stays interactive, and the list matches", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await stub(page, { optimize: [fiveResult(0), fiveResult(1)], confirmScenario: scenarioFive });
    await page.goto("/");
    await page.getByRole("button", { name: "Load fictional sample" }).click();
    await page.getByRole("button", { name: "Confirm scenario" }).click();
    await expect(page.getByTestId("confirmed-note")).toBeVisible();

    const drawMs = await page.evaluate(async () => {
      const button = [...document.querySelectorAll("button")].find((b) => b.textContent === "Optimize") as HTMLButtonElement;
      const t0 = performance.now();
      button.click();
      await new Promise<void>((resolve) => {
        const check = () => (document.querySelectorAll("[data-testid=optimize-result] .react-flow__node").length === 5 && document.querySelectorAll("[data-testid=optimize-result] .react-flow__edge").length >= 5 ? resolve() : requestAnimationFrame(check));
        check();
      });
      return performance.now() - t0;
    });
    const graph = graphOf(page, "optimize-result");
    await expect(graph.locator(".react-flow__node")).toHaveCount(5);
    const arrows = await graph.locator(".react-flow__edge").count();
    await expect(page.getByTestId("assignment-list").locator("li[data-kind='exchange']")).toHaveCount(arrows);
    await expect(page.getByTestId("assignment-list").locator("li")).toHaveCount(6);

    // The picture as first fitted (nothing has been zoomed or animated yet).
    const dir = process.env.C9_SHOTS_DIR;
    if (dir) await graph.screenshot({ path: `${dir}/graph-five-companies.png` });

    const zoomMs = await page.evaluate(async () => {
      const viewport = document.querySelector("[data-testid=optimize-result] .react-flow__viewport") as HTMLElement;
      const button = document.querySelector("[data-testid=optimize-result] .react-flow__controls-zoomin") as HTMLButtonElement;
      const before = viewport.style.transform;
      const t0 = performance.now();
      button.click();
      await new Promise<void>((resolve) => {
        const check = () => (viewport.style.transform !== before ? resolve() : requestAnimationFrame(check));
        check();
      });
      return performance.now() - t0;
    });
    test.info().annotations.push({ type: "C9-PERF", description: `five companies in Chromium: draw ${drawMs.toFixed(0)} ms, zoom ${zoomMs.toFixed(0)} ms` });
    console.info(`[C9-PERF] five companies in Chromium: draw ${drawMs.toFixed(0)} ms, zoom ${zoomMs.toFixed(0)} ms`);
    expect(drawMs).toBeLessThan(3000);
    expect(zoomMs).toBeLessThan(1000);

    // Rapid results: the second result replaces the first without losing the view or a box.
    const transform = await viewportTransform(page, "optimize-result");
    await page.getByRole("button", { name: "Optimize" }).click();
    await expect(page.getByTestId("optimize-announce")).toHaveText(/Optimization finished/);
    await expect(graph.locator(".react-flow__node")).toHaveCount(5);
    expect(await viewportTransform(page, "optimize-result")).toBe(transform);

  });

  test("the same measurement for three companies, for comparison", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await stub(page, { optimize: [LOOP] });
    await page.goto("/");
    await page.getByRole("button", { name: "Load fictional sample" }).click();
    await page.getByRole("button", { name: "Confirm scenario" }).click();
    await expect(page.getByTestId("confirmed-note")).toBeVisible();
    const drawMs = await page.evaluate(async () => {
      const button = [...document.querySelectorAll("button")].find((b) => b.textContent === "Optimize") as HTMLButtonElement;
      const t0 = performance.now();
      button.click();
      await new Promise<void>((resolve) => {
        const check = () => (document.querySelectorAll("[data-testid=optimize-result] .react-flow__node").length === 3 && document.querySelectorAll("[data-testid=optimize-result] .react-flow__edge").length === 3 ? resolve() : requestAnimationFrame(check));
        check();
      });
      return performance.now() - t0;
    });
    const zoomMs = await page.evaluate(async () => {
      const viewport = document.querySelector("[data-testid=optimize-result] .react-flow__viewport") as HTMLElement;
      const button = document.querySelector("[data-testid=optimize-result] .react-flow__controls-zoomin") as HTMLButtonElement;
      const before = viewport.style.transform;
      const t0 = performance.now();
      button.click();
      await new Promise<void>((resolve) => {
        const check = () => (viewport.style.transform !== before ? resolve() : requestAnimationFrame(check));
        check();
      });
      return performance.now() - t0;
    });
    console.info(`[C9-PERF] three companies in Chromium: draw ${drawMs.toFixed(0)} ms, zoom ${zoomMs.toFixed(0)} ms`);
    expect(drawMs).toBeLessThan(3000);
    expect(zoomMs).toBeLessThan(1000);
  });
});
