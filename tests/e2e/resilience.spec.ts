// Browser checks for C7. The API is STUBBED with page.route (hand-written responses, no real
// backend, no AWS). They show how the UI behaves in a real browser, including the CORS rule that
// decides whether a Retry-After header is visible to the page at all.
import { expect, test, type Page } from "@playwright/test";

const BASE_CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "content-type",
  "access-control-allow-methods": "GET, POST, OPTIONS",
};

async function stubApi(page: Page, opts: { exposeRetryAfter: boolean; healthMode?: "fixture" | "live" }) {
  const backend = { mode: opts.healthMode ?? "fixture" }; // what GET /health reports; a test can change it
  const cors = opts.exposeRetryAfter ? { ...BASE_CORS, "access-control-expose-headers": "Retry-After" } : BASE_CORS;
  let optimizeCalls = 0;
  await page.route("http://127.0.0.1:8000/**", async (route) => {
    const request = route.request();
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    const path = new URL(request.url()).pathname;
    const json = (body: unknown, status = 200, extra: Record<string, string> = {}) =>
      route.fulfill({ status, headers: { ...cors, ...extra }, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/health") return json({ status: "ok", schemaVersion: "1", commit: "e2e-stub", mode: backend.mode });
    if (path === "/confirm") return json({ scenario: request.postDataJSON().scenario });
    if (path === "/optimize") {
      optimizeCalls += 1;
      return json({ error: { code: "THROTTLED", message: "Slow down.", fields: [], requestId: "e2e-429" } }, 429, { "Retry-After": "30" });
    }
    return json({}, 404);
  });
  return Object.assign(() => optimizeCalls, { setBackendMode: (mode: "fixture" | "live") => void (backend.mode = mode) });
}

async function toOptimize(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Load fictional sample" }).click();
  await page.getByRole("button", { name: "Confirm scenario" }).click();
  await expect(page.getByTestId("confirmed-note")).toBeVisible();
  await page.getByRole("button", { name: "Optimize" }).click();
}

test("429 with an exposed Retry-After: the retry button counts down and nothing retries by itself", async ({ page }) => {
  const optimizeCalls = await stubApi(page, { exposeRetryAfter: true });
  await toOptimize(page);
  const alert = page.getByTestId("optimize-error");
  await expect(alert).toHaveAttribute("data-kind", "rate_limited");
  await expect(alert).toContainText("asked for a wait of 30 s");
  await expect(alert.getByRole("button", { name: /Try again in \d+ s/ })).toBeDisabled();
  await page.waitForTimeout(800);
  expect(optimizeCalls()).toBe(1);
  await expect(page.getByLabel("Business 1 name")).toHaveValue("DevCo");
});

test("429 whose Retry-After is NOT exposed by CORS: the page cannot see it (backend must expose it)", async ({ page }) => {
  await stubApi(page, { exposeRetryAfter: false });
  await toOptimize(page);
  const alert = page.getByTestId("optimize-error");
  await expect(alert).toHaveAttribute("data-kind", "rate_limited");
  await expect(alert).not.toContainText("asked for a wait");
  await expect(alert.getByRole("button", { name: "Try again" })).toBeEnabled();
});

test("the mode switch exists only when the backend reports the other mode, clears the work product, keeps the badge", async ({ page }) => {
  const stub = await stubApi(page, { exposeRetryAfter: true, healthMode: "fixture" });
  await page.goto("/");
  await page.getByRole("button", { name: "Load fictional sample" }).click();
  await page.getByRole("button", { name: "Confirm scenario" }).click();
  await expect(page.getByTestId("confirmed-note")).toBeVisible();
  const banner = page.getByTestId("mode-banner");

  // App and backend agree: nothing can be clicked into a mismatch.
  await expect(page.getByRole("button", { name: /^Switch/ })).toHaveCount(0);
  await expect(banner.getByRole("button")).toHaveCount(0);

  // The backend is restarted in the other mode and the user checks again.
  stub.setBackendMode("live");
  await page.getByRole("button", { name: "Check the backend again" }).click();
  await expect(page.getByTestId("mode-mismatch")).toContainText("this app is in fixture mode but the backend reports live mode");
  await expect(banner).toContainText("SIMULATED"); // unchanged until the user decides

  await page.getByRole("button", { name: "Switch the app to live mode" }).click();
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("LIVE MODE");
  await expect(page.getByTestId("confirmed-note")).toHaveCount(0);
  await expect(page.getByTestId("mode-switch-note")).toContainText("Switched to live mode");
  await expect(page.getByLabel("Business 1 name")).toHaveValue("DevCo");
  await expect(page.getByTestId("mode-mismatch")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^Switch/ })).toHaveCount(0);
});