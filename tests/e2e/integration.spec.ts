// Real-backend check: the browser talks to the actual local backend (python -m backend.local with
// APP_MODE=fixture, started by playwright.config.ts) and the actual optimizer. NOTHING is stubbed here.
//
// What this covers: confirm, optimize and simulate with the canonical fixture, including the numbers
// from the contract (900 minutes gross savings, 62.5 %, coordination 60 -> 720, designco design 600 ->
// no_exchange, all capacities 0 -> infeasible).
// What it does NOT cover: POST /extract (needs real Bedrock; answers NOT_IMPLEMENTED in fixture mode),
// live mode, and any deployed AWS resource. "Fixture mode" here is the local, explicitly simulated mode.
import { expect, test, type Page } from "@playwright/test";

async function loadAndConfirm(page: Page, coordinationHours?: string) {
  await page.goto("/");
  await expect(page.getByText(/Backend OK/)).toBeVisible();
  await page.getByRole("button", { name: "Load fictional sample" }).click();
  if (coordinationHours) {
    for (const n of [1, 2, 3]) await page.getByLabel(`Business ${n} coordination (hours)`).fill(coordinationHours);
  }
  await page.getByRole("button", { name: "Confirm scenario" }).click();
  await expect(page.getByTestId("confirmed-note")).toBeVisible();
}

test("canonical scenario on the real optimizer: 24 h to 9 h, 900 minutes, three arrows, certificate", async ({ page }) => {
  await loadAndConfirm(page);
  await page.getByRole("button", { name: "Optimize" }).click();
  const result = page.getByTestId("optimize-result");
  await expect(result.getByTestId("result-status")).toHaveText("Beneficial exchange found");
  await expect(result.getByTestId("headline")).toContainText("24 h → 9 h of production");
  await expect(result.getByTestId("savings-summary")).toContainText("900 min (15 h), 62.5% of baseline");
  await expect(result.locator(".react-flow__edge")).toHaveCount(3);
  await expect(result.getByTestId("assignment-list").locator("li[data-kind='exchange']")).toHaveCount(3);
  for (const id of ["devco", "designco", "copyco"]) await expect(result.getByTestId(`row-${id}`)).toContainText("300 min (5 h)");
  // The certificate the real backend sent passes the browser's shape check, and is labelled as fixture.
  await expect(result.getByTestId("certificate-status")).toContainText("checks passed");
  await expect(result.getByTestId("certificate-provenance")).toContainText("SIMULATED");
  await expect(result.getByTestId("certificate-invalid")).toHaveCount(0);
  await expect(result.getByTestId("certificate-missing")).toHaveCount(0);
});

test("coordination of 1 hour each: gross 24 h to 9 h, net 24 h to 12 h", async ({ page }) => {
  await loadAndConfirm(page, "1");
  await page.getByRole("button", { name: "Optimize" }).click();
  const result = page.getByTestId("optimize-result");
  await expect(result.getByTestId("headline")).toContainText("24 h → 9 h of production");
  await expect(result.getByTestId("headline")).toContainText("24 h → 12 h of effort");
  await expect(result.getByTestId("savings-summary")).toContainText("720 min (12 h), 50% of baseline");
  for (const id of ["devco", "designco", "copyco"]) await expect(result.getByTestId(`row-${id}`)).toContainText("240 min (4 h)");
});

test("what-if designco design = 10 h: no_exchange, no arrows, original kept", async ({ page }) => {
  await loadAndConfirm(page);
  await page.getByRole("button", { name: "Optimize" }).click();
  await expect(page.getByTestId("optimize-result")).toBeVisible();
  await page.getByLabel("What-if business").selectOption("designco");
  await page.getByLabel("What-if new value (hours)").fill("10");
  await page.getByRole("button", { name: "Add change" }).click();
  await page.getByRole("button", { name: "Apply these changes" }).click();
  const whatIf = page.getByTestId("whatif-result");
  await expect(whatIf.getByTestId("whatif-status")).toHaveText("Feasible, but no beneficial exchange");
  await expect(whatIf.locator(".react-flow__edge")).toHaveCount(0);
  await expect(whatIf.getByTestId("no-exchange-note")).toBeVisible();
  await expect(page.getByTestId("result-status")).toHaveText("Beneficial exchange found");
});

test("what-if all capacities 0: infeasible with reasons and no figures", async ({ page }) => {
  await loadAndConfirm(page);
  await page.getByRole("button", { name: "Optimize" }).click();
  await expect(page.getByTestId("optimize-result")).toBeVisible();
  for (const business of ["devco", "designco", "copyco"]) {
    await page.getByLabel("What-if business").selectOption(business);
    await page.getByLabel("What-if change type").selectOption("capacity");
    await page.getByLabel("What-if new value (hours)").fill("0");
    await page.getByRole("button", { name: "Add change" }).click();
  }
  await page.getByRole("button", { name: "Apply these changes" }).click();
  const whatIf = page.getByTestId("whatif-result");
  await expect(whatIf.getByTestId("whatif-status")).toContainText("Infeasible");
  await expect(whatIf.getByTestId("whatif-reasons")).toBeVisible();
  await expect(whatIf.getByTestId("no-savings")).toBeVisible();
  await expect(whatIf.getByTestId("certificate-none")).toBeVisible();
});

test("extraction in fixture mode says plainly that it is not available, and keeps the inputs", async ({ page }) => {
  await loadAndConfirm(page);
  await page.getByLabel("Business 1 description").fill("DevCo writes code and needs design work.");
  await page.getByRole("button", { name: "Extract draft" }).click();
  const error = page.getByTestId("extract-error");
  await expect(error).toContainText("NOT_IMPLEMENTED");
  await expect(page.getByLabel("Business 1 name")).toHaveValue("DevCo");
  await expect(page.getByTestId("extraction-review")).toHaveCount(0);
});
