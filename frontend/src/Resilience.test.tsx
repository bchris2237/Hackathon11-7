// Failure and slow-network behaviour, driven by Mock Service Worker (msw/node) answering real
// fetch calls: every documented error code, a slow response, a disconnect, Retry-After, and the
// explicit live/fixture switch. The Results below are hand-written TEST INPUTS shaped like the
// contract, not solver output. Nothing here talks to a real backend or AWS.
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse, type HttpResponseResolver } from "msw";
import { setupServer } from "msw/node";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { Result, Scenario } from "../../contracts/models";
import App from "./App";
import { ApiClient, ApiNetworkError, ApiTimeoutError } from "./lib/api";

const API = "http://127.0.0.1:8000";
const extraction = JSON.parse(readFileSync(resolve(__dirname, "../../fixtures/extraction.json"), "utf8"));
const canonical: Scenario = JSON.parse(readFileSync(resolve(__dirname, "../../fixtures/canonical.json"), "utf8"));

const IDS = ["devco", "designco", "copyco"];
const cert = {
  schemaVersion: "1" as const,
  scenarioDigest: "a".repeat(64),
  allocationDigest: "b".repeat(64),
  verification: "valid" as const,
  assumptions: ["Estimates are supplied by the user and treated as exact."],
  checks: [{ name: "every business within capacity", passed: true as const }],
};
const collaboration: Result = {
  schemaVersion: "1",
  status: "collaboration",
  assignments: [
    { deliverableId: "d-design", providerId: "designco", minutes: 180 },
    { deliverableId: "d-writing", providerId: "copyco", minutes: 180 },
    { deliverableId: "d-coding", providerId: "devco", minutes: 180 },
  ],
  businessMetrics: IDS.map((businessId) => ({
    businessId,
    baselineMinutes: 480,
    productionMinutes: 180,
    coordinationMinutes: 0,
    effortMinutes: 180,
    netSavingsMinutes: 300,
  })),
  totals: { baselineMinutes: 1440, productionMinutes: 540, coordinationMinutes: 0, effortMinutes: 540, grossSavingsMinutes: 900, netSavingsMinutes: 900 },
  reasons: [],
  certificate: cert,
};

const calls: string[] = [];
const count = (what: string) => calls.filter((c) => c === what).length;
const server = setupServer();
server.events.on("request:start", ({ request }) => {
  calls.push(`${request.method} ${new URL(request.url).pathname}`);
});

beforeAll(() => server.listen({ onUnhandledFrame: "error" }));
beforeEach(() => {
  calls.length = 0;
});
afterEach(() => {
  server.resetHandlers();
  vi.unstubAllEnvs();
});
afterAll(() => server.close());

const failure = (status: number, code: string, headers: Record<string, string> = {}) =>
  HttpResponse.json({ error: { code, message: "Failed on purpose.", fields: [], requestId: "req-msw" } }, { status, headers });
const healthOk = (mode: "live" | "fixture" = "fixture") =>
  http.get(`${API}/health`, () => HttpResponse.json({ status: "ok", schemaVersion: "1", commit: "abc", mode }));
const confirmEcho = http.post(`${API}/confirm`, async ({ request }) => HttpResponse.json({ scenario: ((await request.json()) as any).scenario }));
const optimizeWith = (resolver: HttpResponseResolver) => http.post(`${API}/optimize`, resolver);
const optimizeOk = (mode: "live" | "fixture" = "fixture") => optimizeWith(() => HttpResponse.json({ result: collaboration, mode }));

async function readyToOptimize() {
  const user = userEvent.setup();
  render(<App />);
  await user.click(screen.getByRole("button", { name: "Load fictional sample" }));
  await user.click(screen.getByRole("button", { name: "Confirm scenario" }));
  await screen.findByTestId("confirmed-note");
  return user;
}
const clickOptimize = (user: ReturnType<typeof userEvent.setup>) => user.click(screen.getByRole("button", { name: "Optimize" }));
const settle = (ms = 700) => new Promise((r) => setTimeout(r, ms));

describe("every documented error code on a POST, shown by kind and never retried automatically", () => {
  it.each([
    [400, "BAD_JSON", "bad_request", false, undefined],
    [422, "VALIDATION_ERROR", "validation", false, undefined],
    [429, "THROTTLED", "rate_limited", true, "30"],
    [502, "MODEL_FAILURE", "model_unavailable", true, undefined],
    [503, "UNAVAILABLE", "unavailable", true, undefined],
  ])("%i %s -> %s (retry offered: %s)", async (status, code, kind, retryOffered, retryAfter) => {
    server.use(healthOk(), confirmEcho, optimizeWith(() => failure(status, code, retryAfter ? { "Retry-After": retryAfter } : {})));
    const user = await readyToOptimize();
    await clickOptimize(user);

    const alert = await screen.findByTestId("optimize-error");
    expect(alert).toHaveAttribute("data-kind", kind);
    expect(alert).toHaveTextContent(new RegExp(`Optimization failed\\..*${code}.*request req-msw`));
    const retry = within(alert).queryByRole("button", { name: /Try again/ });
    expect(retry !== null).toBe(retryOffered);
    if (retryAfter) {
      expect(retry).toBeDisabled();
      expect(retry).toHaveTextContent("Try again in 30 s");
      expect(alert).toHaveTextContent("asked for a wait of 30 s");
    }
    // Inputs and confirmation survive; nothing was substituted.
    expect(screen.getByLabelText("Business 1 name")).toHaveValue("DevCo");
    expect(screen.getByTestId("confirmed-note")).toBeInTheDocument();
    expect(screen.queryByTestId("optimize-result")).not.toBeInTheDocument();
    expect(screen.queryByText("Calculating the best allocation…")).not.toBeInTheDocument();
    // A mutation is never retried by itself.
    await settle(600);
    expect(count("POST /optimize")).toBe(1);
    expect(screen.getByTestId("mode-banner")).toBeVisible();
  });

  it("the user can retry on purpose once the server's Retry-After has passed", async () => {
    let attempt = 0;
    server.use(
      healthOk(),
      confirmEcho,
      optimizeWith(() => {
        attempt += 1;
        return attempt === 1 ? failure(429, "THROTTLED", { "Retry-After": "1" }) : HttpResponse.json({ result: collaboration, mode: "fixture" });
      }),
    );
    const user = await readyToOptimize();
    await clickOptimize(user);
    const alert = await screen.findByTestId("optimize-error");
    const retry = within(alert).getByRole("button", { name: /Try again in 1 s/ });
    expect(retry).toBeDisabled();
    await waitFor(() => expect(within(alert).getByRole("button", { name: "Try again" })).toBeEnabled(), { timeout: 3000 });
    expect(count("POST /optimize")).toBe(1); // it did not retry by itself
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByTestId("optimize-result")).toBeInTheDocument();
    expect(count("POST /optimize")).toBe(2);
    expect(screen.queryByTestId("optimize-error")).not.toBeInTheDocument();
  });

  it("a model outage on /extract says so, keeps the descriptions, and points to manual entry", async () => {
    server.use(healthOk(), http.post(`${API}/extract`, () => failure(502, "MODEL_FAILURE")));
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("button", { name: "Load fictional sample" }));
    fireEvent.change(screen.getByLabelText("Business 1 description"), { target: { value: "DevCo writes code." } });
    await user.click(screen.getByRole("button", { name: "Extract draft" }));
    const alert = await screen.findByTestId("extract-error");
    expect(alert).toHaveAttribute("data-kind", "model_unavailable");
    expect(alert).toHaveTextContent("The AI model is unavailable");
    expect(alert).toHaveTextContent("type the values into the fields above by hand");
    expect(alert).toHaveTextContent("Your inputs are unchanged.");
    expect(screen.getByLabelText("Business 1 description")).toHaveValue("DevCo writes code.");
    expect(screen.getByLabelText("Business 1 name")).toHaveValue("DevCo");
    expect(screen.queryByTestId("extraction-review")).not.toBeInTheDocument();
  });

  it("a model outage on /simulate points to the manual form and keeps the description", async () => {
    server.use(healthOk(), confirmEcho, optimizeOk(), http.post(`${API}/simulate`, () => failure(502, "MODEL_FAILURE")));
    const user = await readyToOptimize();
    await clickOptimize(user);
    await screen.findByTestId("optimize-result");
    await user.type(screen.getByLabelText("What would you like to try?"), "Make DesignCo slower");
    await user.click(screen.getByRole("button", { name: "Propose changes" }));
    const alert = await screen.findByTestId("whatif-error");
    expect(alert).toHaveAttribute("data-kind", "model_unavailable");
    expect(alert).toHaveTextContent("enter a change yourself with the manual form");
    expect(screen.getByLabelText("What would you like to try?")).toHaveValue("Make DesignCo slower");
  });

  it("/explain failing stays separate from the numbers", async () => {
    server.use(healthOk(), confirmEcho, optimizeOk(), http.post(`${API}/explain`, () => failure(503, "UNAVAILABLE")));
    const user = await readyToOptimize();
    await clickOptimize(user);
    await screen.findByTestId("optimize-result");
    await user.click(screen.getByRole("button", { name: "Explain these results" }));
    const alert = await screen.findByTestId("explain-error");
    expect(alert).toHaveTextContent("The numbers above are unaffected.");
    expect(screen.getByTestId("headline")).toBeInTheDocument();
  });
});

describe("slow responses", () => {
  it("shows work in progress, blocks a second submit, and then shows the result", async () => {
    server.use(
      healthOk(),
      confirmEcho,
      optimizeWith(async () => {
        await delay(400);
        return HttpResponse.json({ result: collaboration, mode: "fixture" });
      }),
    );
    const user = await readyToOptimize();
    await user.dblClick(screen.getByRole("button", { name: "Optimize" }));
    expect(await screen.findByText("Calculating the best allocation…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Optimizing…" })).toBeDisabled();
    expect(await screen.findByTestId("optimize-result", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(screen.queryByText("Calculating the best allocation…")).not.toBeInTheDocument();
    expect(count("POST /optimize")).toBe(1);
  });

  it("the client turns a response slower than its timeout into a timeout error", async () => {
    server.use(
      http.post(`${API}/optimize`, async () => {
        await delay(600);
        return HttpResponse.json({ result: collaboration, mode: "fixture" });
      }),
    );
    const slow = new ApiClient({ apiBaseUrl: API, dataMode: "fixture", timeoutMs: 50 });
    const error = await slow.optimize({ scenario: canonical }).catch((e) => e);
    expect(error).toBeInstanceOf(ApiTimeoutError);
    expect(error.message).toMatch(/did not answer within 0\.05 s/);
    expect(count("POST /optimize")).toBe(1);
  });

  it("editing while a slow request runs drops its late answer", async () => {
    server.use(
      healthOk(),
      confirmEcho,
      optimizeWith(async () => {
        await delay(400);
        return HttpResponse.json({ result: collaboration, mode: "fixture" });
      }),
    );
    const user = await readyToOptimize();
    await clickOptimize(user);
    await screen.findByText("Calculating the best allocation…");
    await user.type(screen.getByLabelText("Business 1 name"), "x");
    await settle(800);
    expect(screen.queryByTestId("optimize-result")).not.toBeInTheDocument();
    expect(screen.queryByTestId("optimize-error")).not.toBeInTheDocument();
    expect(screen.queryByText("Calculating the best allocation…")).not.toBeInTheDocument();
  });
});

describe("disconnects", () => {
  it("a dropped connection is shown as unreachable, with a deliberate retry, and not retried alone", async () => {
    server.use(healthOk(), confirmEcho, optimizeWith(() => HttpResponse.error()));
    const user = await readyToOptimize();
    await clickOptimize(user);
    const alert = await screen.findByTestId("optimize-error");
    expect(alert).toHaveAttribute("data-kind", "unreachable");
    expect(alert).toHaveTextContent("Cannot reach the backend at http://127.0.0.1:8000");
    expect(within(alert).getByRole("button", { name: "Try again" })).toBeEnabled();
    await settle(600);
    expect(count("POST /optimize")).toBe(1);

    server.use(optimizeOk());
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByTestId("optimize-result")).toBeInTheDocument();
  });

  it("the client reports a disconnect as a network error", async () => {
    server.use(http.post(`${API}/optimize`, () => HttpResponse.error()));
    const down = new ApiClient({ apiBaseUrl: API, dataMode: "fixture", healthRetries: 0 });
    await expect(down.optimize({ scenario: canonical })).rejects.toBeInstanceOf(ApiNetworkError);
  });
});

describe("the health check is the one safe read that retries by itself", () => {
  it("recovers from two transient failures without bothering the user", async () => {
    let attempt = 0;
    server.use(
      http.get(`${API}/health`, () => {
        attempt += 1;
        return attempt <= 2 ? failure(503, "UNAVAILABLE") : HttpResponse.json({ status: "ok", schemaVersion: "1", commit: "abc", mode: "fixture" });
      }),
    );
    render(<App />);
    expect(await screen.findByText(/Backend OK/, {}, { timeout: 4000 })).toBeInTheDocument();
    expect(count("GET /health")).toBe(3);
    expect(screen.queryByTestId("health-error")).not.toBeInTheDocument();
  });

  it("after the bounded retries it shows the failure with a manual check-again that works", async () => {
    server.use(http.get(`${API}/health`, () => failure(503, "UNAVAILABLE")));
    const user = userEvent.setup();
    render(<App />);
    const alert = await screen.findByTestId("health-error", {}, { timeout: 4000 });
    expect(alert).toHaveAttribute("data-kind", "unavailable");
    expect(alert).toHaveTextContent("Backend unavailable.");
    expect(count("GET /health")).toBe(3); // 1 try + 2 retries, then it stops
    await settle(600);
    expect(count("GET /health")).toBe(3);

    server.use(healthOk());
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText(/Backend OK/)).toBeInTheDocument();
    expect(screen.queryByTestId("health-error")).not.toBeInTheDocument();
  });

  it("a long Retry-After is handed to the user instead of being waited out", async () => {
    server.use(http.get(`${API}/health`, () => failure(429, "THROTTLED", { "Retry-After": "300" })));
    render(<App />);
    const alert = await screen.findByTestId("health-error");
    expect(alert).toHaveAttribute("data-kind", "rate_limited");
    expect(alert).toHaveTextContent("asked for a wait of 5 min");
    expect(within(alert).getByRole("button", { name: /Try again in 5 min/ })).toBeDisabled();
    await settle(600);
    expect(count("GET /health")).toBe(1);
  });

  it("a disconnected backend ends in the unreachable view", async () => {
    server.use(http.get(`${API}/health`, () => HttpResponse.error()));
    render(<App />);
    const alert = await screen.findByTestId("health-error", {}, { timeout: 4000 });
    expect(alert).toHaveAttribute("data-kind", "unreachable");
    expect(count("GET /health")).toBe(3);
  });
});

describe("explicit switch between live and fixture mode", () => {
  async function liveWithResult() {
    vi.stubEnv("VITE_DATA_MODE", "live");
    server.use(healthOk("live"), confirmEcho, optimizeOk("live"));
    const user = await readyToOptimize();
    expect(screen.getByTestId("mode-banner")).toHaveTextContent("LIVE MODE");
    await clickOptimize(user);
    await screen.findByTestId("optimize-result");
    return user;
  }

  it("clears the live confirmation and result, keeps the typed inputs, and the badge stays visible", async () => {
    const user = await liveWithResult();
    expect(screen.getByTestId("mode-banner")).not.toHaveTextContent("SIMULATED");

    await user.click(screen.getByRole("button", { name: "Switch to fixture mode" }));

    const banner = screen.getByTestId("mode-banner");
    expect(banner).toBeVisible();
    expect(banner).toHaveTextContent("FIXTURE MODE");
    expect(banner).toHaveTextContent("SIMULATED");
    expect(screen.queryByTestId("optimize-result")).not.toBeInTheDocument();
    expect(screen.queryByTestId("confirmed-note")).not.toBeInTheDocument();
    expect(screen.queryByTestId("whatif-result")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Optimize" })).toBeDisabled();
    expect(screen.getByTestId("result-slot")).toHaveTextContent(/No result/);
    expect(screen.getByTestId("mode-switch-note")).toHaveTextContent("Switched to fixture mode");
    expect(screen.getByLabelText("Business 1 name")).toHaveValue("DevCo");
    expect(screen.getByLabelText("Business 2 design estimate (hours)")).toHaveValue("3");
  });

  it("an honest mismatch is shown when the backend is still live; nothing is mixed", async () => {
    const user = await liveWithResult();
    await user.click(screen.getByRole("button", { name: "Switch to fixture mode" }));
    expect(await screen.findByText(/Mode mismatch: this app is in fixture mode but the backend reports live mode/)).toBeInTheDocument();

    // Confirming still works (no mode in that response), but a live-labelled result is refused.
    await user.click(screen.getByRole("button", { name: "Confirm scenario" }));
    await screen.findByTestId("confirmed-note");
    await clickOptimize(user);
    const alert = await screen.findByTestId("optimize-error");
    expect(alert).toHaveAttribute("data-kind", "mode_mismatch");
    expect(alert).toHaveTextContent("Result discarded");
    expect(screen.queryByTestId("optimize-result")).not.toBeInTheDocument();
    expect(within(alert).queryByRole("button", { name: /Try again/ })).not.toBeInTheDocument();
  });

  it("works with a fixture-mode backend and can switch back to live", async () => {
    const user = await liveWithResult();
    server.use(healthOk("fixture"), optimizeOk("fixture"));
    await user.click(screen.getByRole("button", { name: "Switch to fixture mode" }));
    await screen.findByText(/Backend OK/);
    expect(screen.queryByText(/Mode mismatch/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm scenario" }));
    await screen.findByTestId("confirmed-note");
    await clickOptimize(user);
    await screen.findByTestId("optimize-result");

    server.use(healthOk("live"), optimizeOk("live"));
    await user.click(screen.getByRole("button", { name: "Switch to live mode" }));
    expect(screen.getByTestId("mode-banner")).toHaveTextContent("LIVE MODE");
    expect(screen.queryByTestId("optimize-result")).not.toBeInTheDocument();
    expect(screen.getByTestId("mode-switch-note")).toHaveTextContent("Switched to live mode");
  });

  it("a live answer still in flight when the mode is switched is dropped", async () => {
    vi.stubEnv("VITE_DATA_MODE", "live");
    server.use(
      healthOk("live"),
      confirmEcho,
      optimizeWith(async () => {
        await delay(400);
        return HttpResponse.json({ result: collaboration, mode: "live" });
      }),
    );
    const user = await readyToOptimize();
    await clickOptimize(user);
    await screen.findByText("Calculating the best allocation…");
    await user.click(screen.getByRole("button", { name: "Switch to fixture mode" }));
    await settle(800);
    expect(screen.queryByTestId("optimize-result")).not.toBeInTheDocument();
    expect(screen.queryByTestId("optimize-error")).not.toBeInTheDocument();
    expect(screen.queryByText("Calculating the best allocation…")).not.toBeInTheDocument();
    expect(screen.getByTestId("mode-banner")).toHaveTextContent("FIXTURE MODE");
  });

  it("an extraction preview made under the other mode is cleared", async () => {
    vi.stubEnv("VITE_DATA_MODE", "live");
    server.use(healthOk("live"), http.post(`${API}/extract`, () => HttpResponse.json({ ...extraction.response, mode: "live" })));
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("button", { name: "Load fictional sample" }));
    extraction.request.descriptions.forEach((d: any, i: number) => {
      fireEvent.change(screen.getByLabelText(`Business ${i + 1} description`), { target: { value: d.text } });
    });
    await user.click(screen.getByRole("button", { name: "Extract draft" }));
    await screen.findByTestId("extraction-review");
    await user.click(screen.getByRole("button", { name: "Switch to fixture mode" }));
    expect(screen.queryByTestId("extraction-review")).not.toBeInTheDocument();
    expect(screen.getByTestId("mode-banner")).toHaveTextContent("SIMULATED");
  });

  it("the note about the switch disappears at the next edit", async () => {
    const user = await liveWithResult();
    await user.click(screen.getByRole("button", { name: "Switch to fixture mode" }));
    expect(screen.getByTestId("mode-switch-note")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Business 1 name"), "x");
    expect(screen.queryByTestId("mode-switch-note")).not.toBeInTheDocument();
    expect(screen.getByTestId("mode-banner")).toBeVisible();
  });
});
