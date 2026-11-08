// Accessibility and responsive audit (C8), run in real Chromium.
// The API is STUBBED with page.route (hand-written responses, see support.ts). These tests judge
// the UI only. They do not test the backend, Bedrock, or any deployed AWS/Amplify resource.
//
// Set C8_SHOTS_DIR to an existing folder to also write screenshots of the key states.
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { STATES, contrast, reach, stubApi, type StateName } from "./support";

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];
const VIEWPORTS = {
  desktop: { width: 1280, height: 900 },
  mobile360: { width: 360, height: 740 },
};

test.describe("axe: no violations in any screen state", () => {
  for (const [viewportName, size] of Object.entries(VIEWPORTS)) {
    for (const state of STATES) {
      test(`${state} at ${viewportName}`, async ({ page }) => {
        await page.setViewportSize(size);
        await reach(page, state);
        const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
        const summary = results.violations.map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`);
        expect(summary).toEqual([]);
        // Rules axe could not decide on its own are recorded, not silently ignored.
        const undecided = results.incomplete.map((i) => i.id);
        if (undecided.length) test.info().annotations.push({ type: "axe-incomplete", description: undecided.join(", ") });
      });
    }
  }
});

test.describe("responsive: nothing overflows or is clipped on narrow screens", () => {
  for (const width of [360, 320]) {
    for (const state of STATES) {
      test(`${state} at ${width}px`, async ({ page }) => {
        await page.setViewportSize({ width, height: 740 });
        await reach(page, state);
        const report = await page.evaluate(() => {
          const vw = document.documentElement.clientWidth;
          const visible = (el: Element) => {
            const style = getComputedStyle(el);
            return el.getClientRects().length > 0 && style.visibility !== "hidden" && style.display !== "none";
          };
          const label = (el: Element) =>
            `${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""} "${(el.getAttribute("aria-label") || (el as HTMLElement).innerText || (el as HTMLInputElement).name || "").trim().slice(0, 30)}"`;
          const controls = [...document.querySelectorAll("button, input, select, textarea, a[href], [tabindex='0']")].filter(visible);
          const clipped = controls
            .filter((el) => {
              const r = el.getBoundingClientRect();
              return r.left < -0.5 || r.right > vw + 0.5;
            })
            .map(label);
          const tooSmall = controls
            .filter((el) => !el.closest(".skip-link, .react-flow__attribution"))
            .filter((el) => {
              const r = el.getBoundingClientRect();
              return r.width < 24 || r.height < 24;
            })
            .map(label);
          return { pageOverflow: document.documentElement.scrollWidth - vw, clipped, tooSmall };
        });
        expect(report.pageOverflow).toBeLessThanOrEqual(0);
        expect(report.clipped).toEqual([]);
        expect(report.tooSmall).toEqual([]);
      });
    }
  }
});

test("page structure: language, title, one h1, landmarks, unique ids", async ({ page }) => {
  await reach(page, "whatif");
  expect(await page.locator("html").getAttribute("lang")).toBe("en");
  expect((await page.title()).length).toBeGreaterThan(0);
  await expect(page.locator("h1")).toHaveCount(1);
  await expect(page.locator("main")).toHaveCount(1);
  await expect(page.locator("header")).toHaveCount(1);
  const duplicateIds = await page.evaluate(() => {
    const seen = new Map<string, number>();
    document.querySelectorAll("[id]").forEach((el) => seen.set(el.id, (seen.get(el.id) ?? 0) + 1));
    return [...seen].filter(([, n]) => n > 1).map(([id]) => id);
  });
  expect(duplicateIds).toEqual([]);
  // Every form control has an accessible name.
  const unnamed = await page.evaluate(() =>
    [...document.querySelectorAll("input, select, textarea")]
      .filter((el) => getComputedStyle(el).display !== "none")
      .filter((el) => !(el as HTMLInputElement).labels?.length && !el.getAttribute("aria-label") && !el.getAttribute("aria-labelledby"))
      .map((el) => el.outerHTML.slice(0, 80)),
  );
  expect(unnamed).toEqual([]);
});

// ---- keyboard ---------------------------------------------------------------------------

interface Focused {
  name: string;
  tag: string;
  order: number;
  ringVisible: boolean;
  id: string;
}

/** Describes the focused element: its name, its position in the document, and whether a focus ring is drawn. */
async function focused(page: Page): Promise<Focused> {
  return page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null;
    if (!el || el === document.body) return { name: "", tag: "body", order: -1, ringVisible: false, id: "" };
    const style = getComputedStyle(el);
    const name =
      el.getAttribute("aria-label") ||
      (el as HTMLInputElement).labels?.[0]?.textContent ||
      el.textContent ||
      (el as HTMLInputElement).value ||
      "";
    return {
      name: name.replace(/\s+/g, " ").trim(),
      tag: el.tagName.toLowerCase(),
      order: [...document.querySelectorAll("*")].indexOf(el),
      ringVisible: style.outlineStyle !== "none" && parseFloat(style.outlineWidth) >= 2,
      id: el.id,
    };
  });
}

/** Presses Tab until the focused element's name starts with `name`. Fails if the order ever goes backwards. */
async function tabTo(page: Page, name: string, maxPresses = 120) {
  let previous = (await focused(page)).order;
  for (let i = 0; i < maxPresses; i++) {
    await page.keyboard.press("Tab");
    const now = await focused(page);
    expect(now.order, `focus order went backwards while looking for "${name}"`).toBeGreaterThan(previous);
    expect(now.ringVisible, `no visible focus ring on "${now.name}"`).toBe(true);
    previous = now.order;
    if (now.name.startsWith(name)) return now;
  }
  throw new Error(`Could not reach "${name}" by keyboard in ${maxPresses} presses`);
}

test("keyboard only: skip link, load, confirm, optimize, what-if, apply, discard", async ({ page }) => {
  await stubApi(page);
  await page.goto("/");
  await expect(page.getByText(/Backend OK/)).toBeVisible();

  // First stop is the skip link, which becomes visible; Enter moves focus into <main>.
  await page.keyboard.press("Tab");
  const skip = page.getByRole("link", { name: "Skip to workspace" });
  await expect(skip).toBeFocused();
  expect((await skip.boundingBox())!.y).toBeGreaterThanOrEqual(0);
  await page.keyboard.press("Enter");
  expect((await focused(page)).id).toBe("workspace");

  await tabTo(page, "Load fictional sample");
  await page.keyboard.press("Enter");
  await expect(page.getByLabel("Business 1 name")).toHaveValue("DevCo");

  await tabTo(page, "Confirm scenario");
  await page.keyboard.press("Space");
  await expect(page.getByTestId("confirmed-note")).toBeVisible();

  await tabTo(page, "Optimize");
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("optimize-result")).toBeVisible();
  // The result is announced to screen readers through a live region, not by stealing focus.
  await expect(page.getByTestId("optimize-announce")).toHaveText("Optimization finished. Beneficial exchange found.");
  expect((await focused(page)).name).toBe("Optimize");

  // The graph adds no tab stops of its own (only the library's credit link may be focusable).
  await expect(page.locator("[data-testid=network-graph] [tabindex='0']")).toHaveCount(0);

  // Manual what-if with the keyboard alone (type-ahead in the select).
  await tabTo(page, "What-if business");
  await page.keyboard.type("Des");
  await expect(page.getByLabel("What-if business")).toHaveValue("designco");
  await tabTo(page, "What-if new value");
  await page.keyboard.type("10");
  await tabTo(page, "Add change");
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("proposal-changes")).toContainText("DesignCo design estimate: 3 h → 10 h");

  await tabTo(page, "Apply these changes");
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("whatif-result")).toBeVisible();
  // The Apply button vanished; focus must have moved to the result, not been lost.
  const afterApply = await focused(page);
  expect(afterApply.name).toMatch(/^What-if #1/);

  await tabTo(page, "Discard what-if");
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("whatif-result")).toHaveCount(0);
  expect((await focused(page)).name).toBe("What-if simulation");
});

test("keyboard: there is no focus trap (Tab reaches the end of the page and leaves it)", async ({ page }) => {
  await reach(page, "whatif");
  await page.locator("body").click({ position: { x: 1, y: 1 } });
  let presses = 0;
  let left = false;
  for (; presses < 250; presses++) {
    await page.keyboard.press("Tab");
    if ((await focused(page)).tag === "body") {
      left = true;
      break;
    }
  }
  expect(left, `still inside the page after ${presses} Tab presses`).toBe(true);
});

test("keyboard: Shift+Tab returns to the previous control", async ({ page }) => {
  await reach(page, "blank");
  await tabTo(page, "Load fictional sample");
  await page.keyboard.press("Tab");
  const next = await focused(page);
  await page.keyboard.press("Shift+Tab");
  expect((await focused(page)).name).toBe("Load fictional sample");
  expect(next.name).not.toBe("Load fictional sample");
});

// ---- focus after errors and changes -----------------------------------------------------

test("focus moves to the error after a failed action, and the typed inputs stay", async ({ page }) => {
  await reach(page, "optimize-error");
  const alert = page.getByTestId("optimize-error");
  await expect(alert).toBeFocused();
  await expect(alert).toHaveAttribute("role", "alert");
  await expect(page.getByLabel("Business 1 name")).toHaveValue("DevCo");
  // And the retry control is the next stop.
  await page.keyboard.press("Tab");
  expect((await focused(page)).name).toBe("Try again");
});

test("an error nobody asked for (health check on load) does not steal focus", async ({ page }) => {
  await reach(page, "health-error");
  await expect(page.getByTestId("health-error")).not.toBeFocused();
});

test("a bad manual value focuses the field with the problem and describes it", async ({ page }) => {
  await reach(page, "optimized");
  await page.getByLabel("What-if business").selectOption("devco");
  await page.getByLabel("What-if change type").selectOption("capacity");
  await page.getByLabel("What-if new value (hours)").fill("0.001");
  await page.getByRole("button", { name: "Add change" }).click();
  const field = page.getByLabel("What-if new value (hours)");
  await expect(field).toBeFocused();
  await expect(field).toHaveAttribute("aria-invalid", "true");
  const describedBy = await field.getAttribute("aria-describedby");
  expect(describedBy).toBeTruthy();
  await expect(page.locator(`[id="${describedBy}"]`)).toContainText("sub-minute precision");

  // Missing business: focus goes to the select instead.
  await page.getByLabel("What-if business").selectOption("");
  await page.getByLabel("What-if new value (hours)").fill("1");
  await page.getByRole("button", { name: "Add change" }).click();
  await expect(page.getByLabel("What-if business")).toBeFocused();
});

test("focus is never left on a removed element: proposal heading, cancel, remove", async ({ page }) => {
  await reach(page, "optimized");
  await page.getByLabel("What would you like to try?").fill("Make DesignCo design take 10 hours");
  await page.getByRole("button", { name: "Propose changes" }).click();
  await expect(page.getByRole("heading", { name: /Review these changes/ })).toBeFocused();
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("heading", { name: "What-if simulation" })).toBeFocused();

  await page.getByLabel("What-if business").selectOption("devco");
  await page.getByLabel("What-if new value (hours)").fill("5");
  await page.getByRole("button", { name: "Add change" }).click();
  await page.getByRole("button", { name: "Remove change 1" }).click();
  await expect(page.getByRole("button", { name: "Add change" })).toBeFocused();
});

// ---- motion, contrast, graph --------------------------------------------------------------

test("reduced motion: the waiting spinner does not animate", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await reach(page, "sample", { optimizeDelayMs: 1500 });
  await page.getByRole("button", { name: "Confirm scenario" }).click();
  await expect(page.getByTestId("confirmed-note")).toBeVisible();
  await page.getByRole("button", { name: "Optimize" }).click();
  const spinner = page.locator("[aria-busy='true'] > span[aria-hidden='true']");
  await expect(spinner).toBeVisible();
  expect(await spinner.evaluate((el) => getComputedStyle(el).animationName)).toBe("none");
});

test("normal motion: the same spinner does animate (so the reduced-motion check is meaningful)", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await reach(page, "sample", { optimizeDelayMs: 1500 });
  await page.getByRole("button", { name: "Confirm scenario" }).click();
  await expect(page.getByTestId("confirmed-note")).toBeVisible();
  await page.getByRole("button", { name: "Optimize" }).click();
  const spinner = page.locator("[aria-busy='true'] > span[aria-hidden='true']");
  await expect(spinner).toBeVisible();
  expect(await spinner.evaluate((el) => getComputedStyle(el).animationName)).toBe("spin");
});

/**
 * The focus ring colour and the first opaque background behind the control, both as sRGB.
 * Tailwind v4 colours are oklch(), so they are resolved through a canvas pixel instead of parsed as text.
 */
async function ringContrast(page: Page, target: Locator) {
  await page.keyboard.press("Shift"); // keyboard modality, so :focus-visible applies
  await target.focus();
  return target.evaluate((el) => {
    const ctx = document.createElement("canvas").getContext("2d", { willReadFrequently: true })!;
    const rgba = (color: string): number[] => {
      ctx.clearRect(0, 0, 1, 1);
      ctx.fillStyle = "#000";
      ctx.fillStyle = color;
      ctx.fillRect(0, 0, 1, 1);
      const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
      return [r, g, b, a / 255];
    };
    const style = getComputedStyle(el);
    let node: Element | null = el.parentElement;
    let background = [255, 255, 255, 1];
    while (node) {
      const candidate = rgba(getComputedStyle(node).backgroundColor);
      if (candidate[3] > 0.99) {
        background = candidate;
        break;
      }
      node = node.parentElement;
    }
    return { ring: rgba(style.outlineColor), width: parseFloat(style.outlineWidth), style: style.outlineStyle, background };
  });
}

test("focus ring has at least 3:1 contrast against its background (white card, mismatch alert, page background)", async ({ page }) => {
  // "live-mode" state: the app is in fixture mode and the stubbed backend reports live, so the mismatch alert is on screen.
  await reach(page, "live-mode");
  const onCard = await ringContrast(page, page.getByRole("button", { name: "Load fictional sample" }));
  expect(onCard.style).toBe("solid");
  expect(onCard.width).toBeGreaterThanOrEqual(3);
  expect(contrast(onCard.ring, onCard.background)).toBeGreaterThanOrEqual(3);

  const onAlert = await ringContrast(page, page.getByRole("button", { name: "Switch the app to live mode" }));
  expect(onAlert.background.slice(0, 3)).not.toEqual([255, 255, 255]); // sits on the pale red alert
  expect(contrast(onAlert.ring, onAlert.background)).toBeGreaterThanOrEqual(3);

  const onPage = await ringContrast(page, page.getByRole("button", { name: "Check the backend again" }));
  expect(contrast(onPage.ring, onPage.background)).toBeGreaterThanOrEqual(3);
});
test("graph alternative: the text list mirrors every arrow and the graph has a name", async ({ page }) => {
  await reach(page, "optimized");
  const result = page.getByTestId("optimize-result");
  const graph = result.getByTestId("network-graph");
  await expect(graph).toHaveAttribute("aria-label", /3 exchange arrows/);
  await expect(result.locator(".react-flow__edge")).toHaveCount(3);
  const items = result.getByTestId("assignment-list").locator("li[data-kind='exchange']");
  await expect(items).toHaveCount(3);
  await expect(items.first()).toContainText("does");
  await expect(items.first()).toContainText("for");
  // Information is not carried by colour alone: each row names provider, deliverable, minutes and recipient.
  await expect(result.getByTestId("assignment-list")).toContainText("DesignCo does d-design (design, 180 min (3 h)) for DevCo.");

  await reach(page, "whatif");
  const whatIf = page.getByTestId("whatif-result");
  await expect(whatIf.locator(".react-flow__edge")).toHaveCount(0);
  await expect(whatIf.getByTestId("no-exchange-note")).toBeVisible();
  await expect(whatIf.locator("li[data-kind='self']")).toHaveCount(3);
});

test("zoomed text (200% via a 640px-wide window) keeps content reachable without horizontal scrolling", async ({ page }) => {
  await page.setViewportSize({ width: 640, height: 480 });
  await reach(page, "whatif");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});

// ---- SPA refresh (local only) ---------------------------------------------------------------

test("a deep link and a reload serve the app (LOCAL dev server only; this is not an Amplify check)", async ({ page }) => {
  await stubApi(page);
  await page.goto("/some/deep/link?x=1");
  await expect(page.getByRole("heading", { name: "TradeMind" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "TradeMind" })).toBeVisible();
  await expect(page.getByTestId("mode-banner")).toContainText("SIMULATED");
});

// ---- screenshots (optional) -----------------------------------------------------------------

test("screenshots of key states (only when C8_SHOTS_DIR is set)", async ({ page }) => {
  const dir = process.env.C8_SHOTS_DIR;
  test.skip(!dir, "set C8_SHOTS_DIR to write screenshots");
  const jpg = (name: string) => ({ path: `${dir}/${name}.jpg`, type: "jpeg" as const, quality: 50, fullPage: true });
  const plan: [StateName, string, keyof typeof VIEWPORTS][] = [
    ["optimized", "optimized-mobile360", "mobile360"],
    ["whatif", "whatif-desktop", "desktop"],
    ["optimize-error", "error-mobile360", "mobile360"],
  ];
  for (const [state, name, viewport] of plan) {
    await page.setViewportSize(VIEWPORTS[viewport]);
    await reach(page, state);
    await page.screenshot(jpg(name));
    await page.unrouteAll({ behavior: "ignoreErrors" });
  }
  // The graph on its own, at both widths, to judge legibility.
  for (const [viewport, size] of Object.entries(VIEWPORTS)) {
    await page.setViewportSize(size);
    await reach(page, "optimized");
    await page.waitForTimeout(400);
    await page.getByTestId("optimize-result").getByTestId("network-graph").screenshot({ path: `${dir}/graph-${viewport}.png` });
    await page.unrouteAll({ behavior: "ignoreErrors" });
  }
});