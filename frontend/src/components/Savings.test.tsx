// Every Result and Certificate below is a hand-written TEST INPUT shaped like the contract.
// None of it is solver output and the app never contains it.
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Certificate as CertificateData, Result, Scenario } from "../../../contracts/models";
import App from "../App";
import { ApiClient } from "../lib/api";
import Certificate, { checkCertificate } from "./Certificate";
import Savings, { findInconsistencies, hoursText, percent } from "./Savings";

const root = resolve(__dirname, "../../..");
const canonical: Scenario = JSON.parse(readFileSync(resolve(root, "fixtures/canonical.json"), "utf8"));
const IDS = ["devco", "designco", "copyco"];

const cert: CertificateData = {
  schemaVersion: "1",
  scenarioDigest: "a".repeat(64),
  allocationDigest: "b".repeat(64),
  verification: "valid",
  assumptions: ["Estimates are supplied by the user and treated as exact.", "Service quality is assumed equal across providers."],
  checks: [
    { name: "every business within capacity", passed: true },
    { name: "no business worse off", passed: true },
  ],
};

const assignments = [
  { deliverableId: "d-design", providerId: "designco", minutes: 180 },
  { deliverableId: "d-writing", providerId: "copyco", minutes: 180 },
  { deliverableId: "d-coding", providerId: "devco", minutes: 180 },
];

function build(coordination: number, certificate: CertificateData | null = cert): Result {
  const effort = 180 + coordination;
  return {
    schemaVersion: "1",
    status: "collaboration",
    assignments,
    businessMetrics: IDS.map((businessId) => ({
      businessId,
      baselineMinutes: 480,
      productionMinutes: 180,
      coordinationMinutes: coordination,
      effortMinutes: effort,
      netSavingsMinutes: 480 - effort,
    })),
    totals: {
      baselineMinutes: 1440,
      productionMinutes: 540,
      coordinationMinutes: 3 * coordination,
      effortMinutes: 3 * effort,
      grossSavingsMinutes: 900,
      netSavingsMinutes: 1440 - 3 * effort,
    },
    reasons: [],
    certificate,
  };
}

const zeroBaseline: Result = {
  schemaVersion: "1",
  status: "no_exchange",
  assignments: [],
  businessMetrics: IDS.map((businessId) => ({
    businessId,
    baselineMinutes: 0,
    productionMinutes: 0,
    coordinationMinutes: 0,
    effortMinutes: 0,
    netSavingsMinutes: 0,
  })),
  totals: {
    baselineMinutes: 0,
    productionMinutes: 0,
    coordinationMinutes: 0,
    effortMinutes: 0,
    grossSavingsMinutes: 0,
    netSavingsMinutes: 0,
  },
  reasons: [],
  certificate: cert,
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

const client = (mode: "live" | "fixture" = "fixture") => new ApiClient({ apiBaseUrl: "http://api.test", dataMode: mode });

afterEach(() => vi.unstubAllGlobals());

describe("percent and hoursText (no numeric drift)", () => {
  it.each([
    [900, 1440, "62.5%"],
    [720, 1440, "50%"],
    [1440, 1440, "100%"],
    [0, 10, "0%"],
    [1, 3, "33.3%"],
    [2, 3, "66.6%"], // truncated, never rounded up to 66.7
    [1, 7, "14.2%"], // 14.285... truncated
    [9999, 10000, "99.9%"],
    [999999, 1000000, "99.9%"],
    [1_000_000, 1_000_000, "100%"],
  ])("percent(%i, %i) = %s", (part, whole, expected) => {
    expect(percent(part, whole)).toBe(expected);
  });

  it.each([
    [5, 0],
    [0, 0],
    [5, -1],
    [-1, 5],
    [1.5, 3],
    [1, Number.NaN],
  ])("percent(%s, %s) is null: no denominator, no figure", (part, whole) => {
    expect(percent(part, whole)).toBeNull();
  });

  it("hoursText is exact", () => {
    expect(hoursText(1440)).toBe("24 h");
    expect(hoursText(540)).toBe("9 h");
    expect(hoursText(720)).toBe("12 h");
    expect(hoursText(15)).toBe("0.25 h");
    expect(hoursText(10)).toBe("10 min");
  });
});

describe("Savings: canonical, no coordination", () => {
  it("shows 24 h → 9 h and 62.5% gross, net equal to gross", () => {
    render(<Savings scenario={canonical} result={build(0)} client={client()} />);
    expect(screen.getByTestId("headline")).toHaveTextContent("Gross: 24 h → 9 h of production");
    expect(screen.getByTestId("headline")).toHaveTextContent("24 h → 9 h of effort");
    expect(screen.getByTestId("savings-summary")).toHaveTextContent(
      "Gross savings (baseline minus production): 900 min (15 h), 62.5% of baseline. Net savings (after coordination): 900 min (15 h), 62.5% of baseline.",
    );
    expect(within(screen.getByTestId("row-devco")).getByText("300 min (5 h)")).toBeInTheDocument();
  });
});

describe("Savings: 60 minutes coordination each", () => {
  it("gross 24 → 9 h, net 24 → 12 h, and each company nets 4 h", () => {
    render(<Savings scenario={canonical} result={build(60)} client={client()} />);
    expect(screen.getByTestId("headline")).toHaveTextContent("Gross: 24 h → 9 h of production.");
    expect(screen.getByTestId("headline")).toHaveTextContent("Including coordination: 24 h → 12 h of effort.");
    for (const id of IDS) {
      const row = screen.getByTestId(`row-${id}`);
      expect(row).toHaveTextContent("60 min (1 h)"); // coordination
      expect(row).toHaveTextContent("240 min (4 h)"); // effort and net savings
      expect(row).toHaveTextContent("50%");
    }
    const total = screen.getByTestId("row-total");
    expect(total).toHaveTextContent("180 min (3 h)"); // total coordination
    expect(total).toHaveTextContent("720 min (12 h)"); // total effort and net
    expect(screen.getByTestId("savings-summary")).toHaveTextContent(
      "900 min (15 h), 62.5% of baseline. Net savings (after coordination): 720 min (12 h), 50% of baseline.",
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("labels provenance by mode and does not call the figures profit", () => {
    const { unmount } = render(<Savings scenario={canonical} result={build(60)} client={client("fixture")} />);
    expect(screen.getByTestId("savings-provenance")).toHaveTextContent(/SIMULATED \(fixture mode\) backend/);
    expect(screen.getByTestId("savings-provenance")).toHaveTextContent(/derived in this browser/);
    expect(screen.getByTestId("savings-provenance")).toHaveTextContent(/not profit/);
    unmount();
    render(<Savings scenario={canonical} result={build(60)} client={client("live")} />);
    expect(screen.getByTestId("savings-provenance")).toHaveTextContent(/live backend/);
    expect(screen.getByTestId("savings-provenance")).not.toHaveTextContent(/SIMULATED/);
  });
});

describe("Savings: edge cases", () => {
  it("zero baseline: no percentage, no NaN or Infinity", () => {
    render(<Savings scenario={canonical} result={zeroBaseline} client={client()} />);
    const text = screen.getByTestId("savings").textContent ?? "";
    expect(text).not.toMatch(/NaN|Infinity|undefined/);
    expect(screen.getByTestId("savings-summary")).toHaveTextContent("total baseline is 0");
    expect(screen.getByTestId("savings-summary")).not.toHaveTextContent("%");
    expect(within(screen.getByTestId("row-devco")).getByText("n/a (baseline is 0)")).toBeInTheDocument();
  });

  it("infeasible (null totals) shows no figures at all", () => {
    render(<Savings scenario={canonical} result={infeasible} client={client()} />);
    expect(screen.getByTestId("no-savings")).toHaveTextContent(/nothing was computed/);
    expect(screen.queryByTestId("metrics")).not.toBeInTheDocument();
    expect(screen.queryByTestId("headline")).not.toBeInTheDocument();
    expect(screen.queryByTestId("explain")).not.toBeInTheDocument();
  });

  it("flags a result whose own figures disagree instead of presenting it as fine", () => {
    const bad = build(0);
    bad.totals!.netSavingsMinutes = 1000;
    expect(findInconsistencies(bad)).toContain("Net savings is not baseline minus effort.");
    expect(findInconsistencies(bad)).toContain("Per-business net savings does not add up to the total.");
    render(<Savings scenario={canonical} result={bad} client={client()} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/disagree with each other/);
  });

  it("a consistent canonical result has no inconsistencies", () => {
    expect(findInconsistencies(build(0))).toEqual([]);
    expect(findInconsistencies(build(60))).toEqual([]);
    expect(findInconsistencies(zeroBaseline)).toEqual([]);
    expect(findInconsistencies(infeasible)).toEqual([]);
  });
});

describe("Certificate", () => {
  it("renders checks, digests and assumptions with live provenance", () => {
    render(<Certificate result={build(60)} mode="live" />);
    expect(screen.getByTestId("certificate-status")).toHaveTextContent("2 checks passed");
    expect(screen.getByTestId("certificate-checks")).toHaveTextContent("no business worse off: passed");
    expect(screen.getByTestId("scenario-digest")).toHaveTextContent("a".repeat(64));
    expect(screen.getByTestId("allocation-digest")).toHaveTextContent("b".repeat(64));
    expect(screen.getByTestId("certificate-assumptions")).toHaveTextContent(/quality is assumed equal/);
    expect(screen.getByTestId("certificate-provenance")).toHaveTextContent("live backend");
    expect(screen.getByTestId("certificate-provenance")).not.toHaveTextContent("SIMULATED");
  });

  it("labels fixture mode as SIMULATED", () => {
    render(<Certificate result={build(0)} mode="fixture" />);
    expect(screen.getByTestId("certificate-provenance")).toHaveTextContent(/SIMULATED \(fixture mode\)\. Not a live verification/);
  });

  it("states what the certificate is not", () => {
    render(<Certificate result={build(0)} mode="live" />);
    const limits = screen.getByTestId("certificate-limits");
    expect(limits).toHaveTextContent(/not a signature/);
    expect(limits).toHaveTextContent(/not tamper-proof/);
    expect(limits).toHaveTextContent(/not an identity credential/);
    expect(limits).toHaveTextContent(/not a guarantee of profit, payment or service quality/);
  });

  it("a missing certificate on a feasible result is flagged as NOT verified", () => {
    render(<Certificate result={build(0, null)} mode="live" />);
    expect(screen.getByTestId("certificate-missing")).toHaveTextContent(/NOT verified/);
    expect(screen.queryByTestId("certificate-status")).not.toBeInTheDocument();
    expect(screen.queryByTestId("scenario-digest")).not.toBeInTheDocument();
  });

  it("infeasible has no certificate and says why", () => {
    render(<Certificate result={infeasible} mode="live" />);
    expect(screen.getByTestId("certificate-none")).toHaveTextContent(/no certificate is issued/);
    expect(screen.queryByTestId("certificate-missing")).not.toBeInTheDocument();
  });

  const invalid: [string, unknown][] = [
    ["short digest", { ...cert, scenarioDigest: "abc" }],
    ["uppercase digest", { ...cert, allocationDigest: "A".repeat(64) }],
    ["verification not valid", { ...cert, verification: "invalid" }],
    ["wrong schema version", { ...cert, schemaVersion: "2" }],
    ["a failed check", { ...cert, checks: [{ name: "capacity", passed: false }] }],
    ["no checks", { ...cert, checks: [] }],
    ["unnamed check", { ...cert, checks: [{ name: "", passed: true }] }],
    ["non-string assumption", { ...cert, assumptions: [1] }],
    ["unknown field", { ...cert, signature: "trust me" }],
    ["not an object", "valid"],
  ];
  it.each(invalid)("an invalid certificate (%s) is never shown as verified", (_name, value) => {
    expect(checkCertificate(value).ok).toBe(false);
    render(<Certificate result={{ ...build(0), certificate: value as CertificateData }} mode="live" />);
    expect(screen.getByTestId("certificate-invalid")).toHaveTextContent(/NOT shown as verified/);
    expect(screen.queryByTestId("certificate-status")).not.toBeInTheDocument();
    expect(screen.queryByTestId("scenario-digest")).not.toBeInTheDocument();
  });

  it("accepts the valid sample", () => {
    expect(checkCertificate(cert).ok).toBe(true);
  });
});

// ---- through the screen, with a stubbed backend ------------------------------------------

const HEALTH = { status: "ok", schemaVersion: "1", commit: "abc", mode: "fixture" };
interface Routes {
  optimize: Response | (() => Response | Promise<Response>);
  explain?: () => Response | Promise<Response>;
}
function stubBackend(routes: Routes) {
  const calls: { path: string; body: any }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const path = new URL(url).pathname;
      if (path === "/health") return Response.json(HEALTH);
      const body = JSON.parse(init.body as string);
      calls.push({ path, body });
      if (path === "/confirm") return Response.json({ scenario: body.scenario });
      if (path === "/optimize") return typeof routes.optimize === "function" ? routes.optimize() : routes.optimize.clone();
      if (path === "/explain" && routes.explain) return routes.explain();
      throw new TypeError("no route " + path);
    }),
  );
  return calls;
}

async function optimizeWithCoordination() {
  const user = userEvent.setup();
  render(<App />);
  await user.click(screen.getByRole("button", { name: "Load fictional sample" }));
  [1, 2, 3].forEach((n) => fireEvent.change(screen.getByLabelText(`Business ${n} coordination (hours)`), { target: { value: "1" } }));
  await user.click(screen.getByRole("button", { name: "Confirm scenario" }));
  await screen.findByTestId("confirmed-note");
  await user.click(screen.getByRole("button", { name: "Optimize" }));
  await screen.findByTestId("optimize-result");
  return user;
}

describe("through the screen", () => {
  it("sends 60 minutes of coordination and renders savings and certificate from the response", async () => {
    const calls = stubBackend({ optimize: () => Response.json({ result: build(60), mode: "fixture" }) });
    await optimizeWithCoordination();
    const sent = calls.find((c) => c.path === "/optimize")!;
    expect(sent.body.scenario.businesses.map((b: any) => b.coordinationMinutes)).toEqual([60, 60, 60]);
    expect(screen.getByTestId("headline")).toHaveTextContent("24 h → 12 h of effort");
    expect(screen.getByTestId("certificate-status")).toHaveTextContent("2 checks passed");
    expect(screen.getByTestId("certificate-provenance")).toHaveTextContent(/SIMULATED/);
  });

  it("shows a missing certificate as not verified on the screen", async () => {
    stubBackend({ optimize: () => Response.json({ result: build(60, null), mode: "fixture" }) });
    await optimizeWithCoordination();
    expect(screen.getByTestId("certificate-missing")).toBeInTheDocument();
    expect(screen.getByTestId("headline")).toBeInTheDocument(); // numbers still shown, but not as verified
  });

  it("explanation prose is subordinate and labelled", async () => {
    const calls = stubBackend({
      optimize: () => Response.json({ result: build(60), mode: "fixture" }),
      explain: () => Response.json({ text: "Each company saves 4 hours.", mode: "fixture" }),
    });
    const user = await optimizeWithCoordination();
    await user.click(screen.getByRole("button", { name: "Explain these results" }));
    expect(await screen.findByTestId("explain-text")).toHaveTextContent("Each company saves 4 hours.");
    expect(screen.getByTestId("explain-text")).toHaveTextContent(/the figures are authoritative/);
    expect(screen.getByTestId("explain-text")).toHaveTextContent(/SIMULATED/);
    const body = calls.find((c) => c.path === "/explain")!.body;
    expect(Object.keys(body).sort()).toEqual(["result", "scenario"]);
  });

  it("an /explain failure is reported on its own and the numbers stay", async () => {
    stubBackend({
      optimize: () => Response.json({ result: build(60), mode: "fixture" }),
      explain: () =>
        Response.json({ error: { code: "MODEL_FAILURE", message: "Model failed.", fields: [], requestId: "r-7" } }, { status: 502 }),
    });
    const user = await optimizeWithCoordination();
    await user.click(screen.getByRole("button", { name: "Explain these results" }));
    const alert = await screen.findByText(/Explanation unavailable/);
    expect(alert).toHaveTextContent(/MODEL_FAILURE.*r-7/);
    expect(alert).toHaveTextContent(/numbers above are unaffected/);
    expect(screen.queryByTestId("explain-text")).not.toBeInTheDocument();
    expect(screen.getByTestId("headline")).toHaveTextContent("24 h → 12 h");
    expect(screen.getByTestId("certificate-status")).toBeInTheDocument();
  });

  it("rejects an empty or non-string explanation", async () => {
    stubBackend({
      optimize: () => Response.json({ result: build(60), mode: "fixture" }),
      explain: () => Response.json({ text: "", mode: "fixture" }),
    });
    const user = await optimizeWithCoordination();
    await user.click(screen.getByRole("button", { name: "Explain these results" }));
    expect(await screen.findByText(/does not match the v1 contract/)).toBeInTheDocument();
  });

  it("an edit removes savings, certificate and any explanation", async () => {
    stubBackend({
      optimize: () => Response.json({ result: build(60), mode: "fixture" }),
      explain: () => Response.json({ text: "Fine.", mode: "fixture" }),
    });
    const user = await optimizeWithCoordination();
    await user.click(screen.getByRole("button", { name: "Explain these results" }));
    await screen.findByTestId("explain-text");
    await user.type(screen.getByLabelText("Business 1 name"), "x");
    expect(screen.queryByTestId("savings")).not.toBeInTheDocument();
    expect(screen.queryByTestId("certificate")).not.toBeInTheDocument();
    expect(screen.queryByTestId("explain-text")).not.toBeInTheDocument();
  });
});
