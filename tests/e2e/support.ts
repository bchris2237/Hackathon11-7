// Shared helpers for the accessibility specs. The API is STUBBED with page.route: every body
// below is a hand-written test input shaped like the v1 contract. It is not solver output, and
// no real backend, Bedrock or AWS resource is involved.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { Page } from "@playwright/test";

const root = resolve(__dirname, "../..");
export const extraction = JSON.parse(readFileSync(resolve(root, "fixtures/extraction.json"), "utf8"));

const IDS = ["devco", "designco", "copyco"];
const cert = {
  schemaVersion: "1",
  scenarioDigest: "a1b2c3d4".repeat(8),
  allocationDigest: "0f9e8d7c".repeat(8),
  verification: "valid",
  assumptions: ["Estimates are supplied by the user and treated as exact.", "Service quality is assumed equal across providers."],
  checks: [
    { name: "every business within capacity", passed: true },
    { name: "no business worse off", passed: true },
  ],
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
export const collaboration = {
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
export const noExchange = {
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

const DESIGN_CHANGE = { kind: "estimate", businessId: "designco", service: "design", minutes: 600 };
const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "content-type",
  "access-control-allow-methods": "GET, POST, OPTIONS",
  "access-control-expose-headers": "Retry-After",
};

export interface StubOptions {
  healthMode?: "fixture" | "live";
  /** Make POST /optimize fail with this status and the v1 envelope. */
  optimizeFails?: number;
  /** Make GET /health fail with 503. */
  healthFails?: boolean;
  /** Hold POST /optimize this long before answering (to look at the waiting view). */
  optimizeDelayMs?: number;
}

export async function stubApi(page: Page, options: StubOptions = {}) {
  const mode = options.healthMode ?? "fixture";
  await page.route("http://127.0.0.1:8000/**", async (route) => {
    const request = route.request();
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const path = new URL(request.url()).pathname;
    const send = (body: unknown, status = 200) =>
      route.fulfill({ status, headers: CORS, contentType: "application/json", body: JSON.stringify(body) });
    const fail = (status: number, code: string) =>
      send({ error: { code, message: "Failed on purpose.", fields: [], requestId: "e2e-1" } }, status);
    if (path === "/health") {
      return options.healthFails ? fail(503, "UNAVAILABLE") : send({ status: "ok", schemaVersion: "1", commit: "e2e-stub", mode });
    }
    const body = request.postDataJSON();
    if (path === "/extract") return send({ ...extraction.response, mode });
    if (path === "/confirm") return send({ scenario: body.scenario });
    if (path === "/optimize" && options.optimizeDelayMs) await new Promise((r) => setTimeout(r, options.optimizeDelayMs));
    if (path === "/optimize") return options.optimizeFails ? fail(options.optimizeFails, "UNAVAILABLE") : send({ result: collaboration, mode });
    if (path === "/explain") return send({ text: "Each company saves five hours by trading its weaker service for its stronger one.", mode });
    if (path === "/simulate") {
      if (!body.apply) return send({ proposedChanges: [DESIGN_CHANGE], requiresConfirmation: true, scenario: body.scenario, result: null, mode });
      const scenario = structuredClone(body.scenario);
      scenario.businesses[1].estimates.find((e: any) => e.service === "design").minutes = 600;
      return send({ proposedChanges: body.changes, requiresConfirmation: false, scenario, result: noExchange, mode });
    }
    return send({}, 404);
  });
}

/** WCAG contrast ratio of two sRGB colours given as [r, g, b, a?]. A translucent foreground is composited onto the background. */
export function contrast(foreground: number[], background: number[]): number {
  const [fr, fg, fb, fa = 1] = foreground;
  const [br, bg, bb] = background;
  const mix = (f: number, b: number) => f * fa + b * (1 - fa);
  const lum = (r: number, g: number, b: number) => {
    const ch = [r, g, b].map((v) => {
      const s = v / 255;
      return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2];
  };
  const l1 = lum(mix(fr, br), mix(fg, bg), mix(fb, bb));
  const l2 = lum(br, bg, bb);
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
}

import { expect } from "@playwright/test";

export const STATES = [
  "blank",
  "sample",
  "extraction",
  "optimized",
  "explained",
  "proposal",
  "whatif",
  "optimize-error",
  "health-error",
  "live-mode",
] as const;
export type StateName = (typeof STATES)[number];

/** Drives the real UI (with the stubbed API) into a named screen state. */
export async function reach(page: Page, state: StateName, extra: StubOptions = {}) {
  await stubApi(page, {
    ...extra,
    healthFails: state === "health-error",
    optimizeFails: state === "optimize-error" ? 503 : undefined,
    healthMode: state === "live-mode" ? "live" : "fixture",
  });
  await page.goto("/");
  if (state === "health-error") {
    await expect(page.getByTestId("health-error")).toBeVisible({ timeout: 6000 });
    return;
  }
  await expect(page.getByText(/Backend OK/)).toBeVisible();
  if (state === "blank") return;
  if (state === "live-mode") {
    await page.getByRole("button", { name: "Load fictional sample" }).click();
    return;
  }

  await page.getByRole("button", { name: "Load fictional sample" }).click();
  if (state === "sample") return;

  if (state === "extraction") {
    for (let i = 0; i < extraction.request.descriptions.length; i++) {
      await page.getByLabel(`Business ${i + 1} description`).fill(extraction.request.descriptions[i].text);
    }
    await page.getByRole("button", { name: "Extract draft" }).click();
    await expect(page.getByTestId("extraction-review")).toBeVisible();
    return;
  }

  await page.getByRole("button", { name: "Confirm scenario" }).click();
  await expect(page.getByTestId("confirmed-note")).toBeVisible();
  await page.getByRole("button", { name: "Optimize" }).click();
  if (state === "optimize-error") {
    await expect(page.getByTestId("optimize-error")).toBeVisible();
    return;
  }
  await expect(page.getByTestId("optimize-result")).toBeVisible();
  if (state === "optimized") return;

  if (state === "explained") {
    await page.getByRole("button", { name: "Explain these results" }).click();
    await expect(page.getByTestId("explain-text")).toBeVisible();
    return;
  }

  await page.getByLabel("What would you like to try?").fill("Make DesignCo design take 10 hours");
  await page.getByRole("button", { name: "Propose changes" }).click();
  await expect(page.getByTestId("proposal")).toBeVisible();
  if (state === "proposal") return;

  await page.getByRole("button", { name: "Apply these changes" }).click();
  await expect(page.getByTestId("whatif-result")).toBeVisible();
}
