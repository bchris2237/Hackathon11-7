// Browser check of the critical path: sample -> confirm -> optimize -> what-if (approve) -> compare.
// The API is STUBBED with page.route and the results below are hand-written test inputs, not
// solver output. This verifies the UI (including the React Flow graph, which jsdom cannot lay
// out). It does NOT verify the backend, Bedrock or any deployed AWS resource.
import { expect, test, type Page, type Route } from "@playwright/test";

const IDS = ["devco", "designco", "copyco"];
const cert = {
  schemaVersion: "1",
  scenarioDigest: "a".repeat(64),
  allocationDigest: "b".repeat(64),
  verification: "valid",
  assumptions: ["Estimates are supplied by the user and treated as exact."],
  checks: [{ name: "every business within capacity", passed: true }],
};
const metrics = (production: number) =>
  IDS.map((businessId) => ({
    businessId,
    baselineMinutes: 480,
    productionMinutes: production,
    coordinationMinutes: 0,
    effortMinutes: production,
    netSavingsMinutes: 480 - production,
  }));
const collaboration = {
  schemaVersion: "1",
  status: "collaboration",
  assignments: [
    { deliverableId: "d-design", providerId: "designco", minutes: 180 },
    { deliverableId: "d-writing", providerId: "copyco", minutes: 180 },
    { deliverableId: "d-coding", providerId: "devco", minutes: 180 },
  ],
  businessMetrics: metrics(180),
  totals: { baselineMinutes: 1440, productionMinutes: 540, coordinationMinutes: 0, effortMinutes: 540, grossSavingsMinutes: 900, netSavingsMinutes: 900 },
  reasons: [],
  certificate: cert,
};
const noExchange = {
  schemaVersion: "1",
  status: "no_exchange",
  assignments: [
    { deliverableId: "d-design", providerId: "devco", minutes: 480 },
    { deliverableId: "d-writing", providerId: "designco", minutes: 480 },
    { deliverableId: "d-coding", providerId: "copyco", minutes: 480 },
  ],
  businessMetrics: metrics(480),
  totals: { baselineMinutes: 1440, productionMinutes: 1440, coordinationMinutes: 0, effortMinutes: 1440, grossSavingsMinutes: 0, netSavingsMinutes: 0 },
  reasons: [{ code: "NO_BENEFICIAL_EXCHANGE", message: "No exchange improves on self-production." }],
  certificate: cert,
};

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "content-type",
  "access-control-allow-methods": "GET, POST, OPTIONS",
};

async function stubApi(page: Page) {
  const simulateBodies: any[] = [];
  await page.route("http://127.0.0.1:8000/**", async (route: Route) => {
    const request = route.request();
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const path = new URL(request.url()).pathname;
    const json = (body: unknown) => route.fulfill({ status: 200, headers: CORS, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/health") return json({ status: "ok", schemaVersion: "1", commit: "e2e-stub", mode: "fixture" });
    const body = request.postDataJSON();
    if (path === "/confirm") return json({ scenario: body.scenario });
    if (path === "/optimize") return json({ result: collaboration, mode: "fixture" });
    if (path === "/simulate") {
      simulateBodies.push(body);
      const scenario = structuredClone(body.scenario);
      scenario.businesses[1].estimates.find((e: any) => e.service === "design").minutes = 600;
      return json({ proposedChanges: body.changes, requiresConfirmation: false, scenario, result: noExchange, mode: "fixture" });
    }
    return route.fulfill({ status: 404, headers: CORS, contentType: "application/json", body: "{}" });
  });
  return simulateBodies;
}

test("sample -> confirm -> optimize shows a three-edge loop; approved what-if shows no loop and keeps the original", async ({ page }) => {
  const simulateBodies = await stubApi(page);
  await page.goto("/");
  await expect(page.getByTestId("mode-banner")).toContainText("SIMULATED");

  await page.getByRole("button", { name: "Load fictional sample" }).click();
  await page.getByRole("button", { name: "Confirm scenario" }).click();
  await expect(page.getByTestId("confirmed-note")).toBeVisible();
  await page.getByRole("button", { name: "Optimize" }).click();

  await expect(page.getByTestId("result-status")).toHaveText("Beneficial exchange found");
  const original = page.getByTestId("optimize-result");
  await expect(original.locator(".react-flow__edge")).toHaveCount(3);
  await expect(original.getByTestId("assignment-list").getByRole("listitem")).toHaveCount(3);
  await expect(original.getByTestId("headline")).toContainText("24 h → 9 h");

  // Manual what-if, reviewed before anything is sent.
  await page.getByLabel("What-if business").selectOption("designco");
  await page.getByLabel("What-if new value (hours)").fill("10");
  await page.getByRole("button", { name: "Add change" }).click();
  await expect(page.getByTestId("proposal")).toContainText("NOT applied yet");
  await expect(page.getByTestId("proposal-changes")).toContainText("DesignCo design estimate: 3 h → 10 h");
  expect(simulateBodies).toHaveLength(0);

  await page.getByRole("button", { name: "Apply these changes" }).click();
  const whatIf = page.getByTestId("whatif-result");
  await expect(whatIf.getByTestId("whatif-status")).toHaveText("Feasible, but no beneficial exchange");
  await expect(whatIf.locator(".react-flow__edge")).toHaveCount(0);
  await expect(whatIf.getByTestId("no-exchange-note")).toBeVisible();
  expect(simulateBodies).toHaveLength(1);
  expect(simulateBodies[0].apply).toBe(true);
  expect(simulateBodies[0]).not.toHaveProperty("text");

  // The original is still there, unchanged.
  await expect(page.getByTestId("result-status")).toHaveText("Beneficial exchange found");
  await expect(original.locator(".react-flow__edge")).toHaveCount(3);

  // Keyboard focus is visible on a control (a key press first, so focus-visible applies).
  await page.keyboard.press("Shift");
  await page.getByRole("button", { name: "Discard what-if" }).focus();
  const outline = await page.getByRole("button", { name: "Discard what-if" }).evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(outline).not.toBe("none");
});

test("a failing live-style backend response is shown and nothing is substituted", async ({ page }) => {
  await page.route("http://127.0.0.1:8000/**", async (route) => {
    const request = route.request();
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const path = new URL(request.url()).pathname;
    if (path === "/health") {
      return route.fulfill({ status: 200, headers: CORS, contentType: "application/json", body: JSON.stringify({ status: "ok", schemaVersion: "1", commit: "e2e-stub", mode: "fixture" }) });
    }
    if (path === "/confirm") {
      const body = request.postDataJSON();
      return route.fulfill({ status: 200, headers: CORS, contentType: "application/json", body: JSON.stringify({ scenario: body.scenario }) });
    }
    return route.fulfill({
      status: 503,
      headers: CORS,
      contentType: "application/json",
      body: JSON.stringify({ error: { code: "UNAVAILABLE", message: "Solver down.", fields: [], requestId: "e2e-1" } }),
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Load fictional sample" }).click();
  await page.getByRole("button", { name: "Confirm scenario" }).click();
  await expect(page.getByTestId("confirmed-note")).toBeVisible();
  await page.getByRole("button", { name: "Optimize" }).click();
  await expect(page.getByText(/Optimization failed/)).toContainText("UNAVAILABLE");
  await expect(page.getByTestId("optimize-result")).toHaveCount(0);
  await expect(page.getByLabel("Business 1 name")).toHaveValue("DevCo");
});
