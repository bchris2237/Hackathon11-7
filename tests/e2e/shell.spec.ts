// Phase 0 smoke test: the shell renders, labels fixture mode, and reaches the real local backend.
// Developer 3 replaces/extends this from prompt C1 on.
import { expect, test } from "@playwright/test";

test("shell shows the fixture banner and a healthy backend", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "TradeMind" })).toBeVisible();
  await expect(page.getByTestId("mode-banner")).toContainText("FIXTURE MODE");
  await expect(page.getByTestId("health-state")).toContainText("Backend OK");
  await expect(page.getByTestId("health-state")).toContainText("fixture");
  await expect(page.getByText("Not built yet")).toBeVisible();
});

test("shell reports the backend as unavailable when it is unreachable", async ({ page }) => {
  await page.route("**/health", (route) => route.abort("connectionrefused"));
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("Backend unavailable");
});
