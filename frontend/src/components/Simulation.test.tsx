// Results below are hand-written TEST INPUTS shaped like the contract, standing in for a stubbed
// backend. They are not solver output and the app never contains them. The stub "server"
// applies each change explicitly and independently of the code under test.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Certificate as CertificateData, Result, Scenario } from "../../../contracts/models";
import App from "../App";

const root = resolve(__dirname, "../../..");
const disruption = JSON.parse(readFileSync(resolve(root, "fixtures/disruption.json"), "utf8"));
const IDS = ["devco", "designco", "copyco"];

const cert: CertificateData = {
  schemaVersion: "1",
  scenarioDigest: "a".repeat(64),
  allocationDigest: "b".repeat(64),
  verification: "valid",
  assumptions: ["Estimates are supplied by the user and treated as exact."],
  checks: [{ name: "every business within capacity", passed: true }],
};

const metrics = (production: number, baseline: number) =>
  IDS.map((businessId) => ({
    businessId,
    baselineMinutes: baseline,
    productionMinutes: production,
    coordinationMinutes: 0,
    effortMinutes: production,
    netSavingsMinutes: baseline - production,
  }));

const baseResult: Result = {
  schemaVersion: "1",
  status: "collaboration",
  assignments: [
    { deliverableId: "d-design", providerId: "designco", minutes: 180 },
    { deliverableId: "d-writing", providerId: "copyco", minutes: 180 },
    { deliverableId: "d-coding", providerId: "devco", minutes: 180 },
  ],
  businessMetrics: metrics(180, 480),
  totals: { baselineMinutes: 1440, productionMinutes: 540, coordinationMinutes: 0, effortMinutes: 540, grossSavingsMinutes: 900, netSavingsMinutes: 900 },
  reasons: [],
  certificate: cert,
};

const noExchange: Result = {
  schemaVersion: "1",
  status: "no_exchange",
  assignments: [
    { deliverableId: "d-design", providerId: "devco", minutes: 480 },
    { deliverableId: "d-writing", providerId: "designco", minutes: 480 },
    { deliverableId: "d-coding", providerId: "copyco", minutes: 480 },
  ],
  businessMetrics: metrics(480, 480),
  totals: { baselineMinutes: 1440, productionMinutes: 1440, coordinationMinutes: 0, effortMinutes: 1440, grossSavingsMinutes: 0, netSavingsMinutes: 0 },
  reasons: [{ code: "NO_BENEFICIAL_EXCHANGE", message: "No exchange improves on self-production." }],
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

// What a correct server would return as the changed scenario (written out, not computed by app code).
const designco600 = (s: Scenario): Scenario => {
  const copy = structuredClone(s);
  copy.businesses.find((b) => b.id === "designco")!.estimates.find((e) => e.service === "design")!.minutes = 600;
  return copy;
};
const allCapacityZero = (s: Scenario): Scenario => {
  const copy = structuredClone(s);
  copy.businesses.forEach((b) => (b.capacityMinutes = 0));
  return copy;
};

const DESIGN_CHANGE = { kind: "estimate", businessId: "designco", service: "design", minutes: 600 };
const HEALTH = { status: "ok", schemaVersion: "1", commit: "abc", mode: "fixture" };

type SimBody = { scenario: Scenario; changes: any[]; text?: string; apply: boolean };
type SimHandler = (body: SimBody, call: number) => Response | Promise<Response>;

function stubBackend(simulate: SimHandler) {
  const sims: SimBody[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const path = new URL(url).pathname;
      if (path === "/health") return Response.json(HEALTH);
      const body = JSON.parse(init.body as string);
      if (path === "/confirm") return Response.json({ scenario: body.scenario });
      if (path === "/optimize") return Response.json({ result: baseResult, mode: "fixture" });
      if (path === "/simulate") {
        sims.push(body);
        return simulate(body, sims.length);
      }
      throw new TypeError("no route " + path);
    }),
  );
  return sims;
}

const sim = (scenario: Scenario, result: Result | null, proposedChanges: unknown[] = [], mode = "fixture") =>
  Response.json({ proposedChanges, requiresConfirmation: result === null, scenario, result, mode });
const envelope = (status: number, code: string) =>
  Response.json({ error: { code, message: "Failed on purpose.", fields: [], requestId: "r-1" } }, { status });

afterEach(() => vi.unstubAllGlobals());

async function optimized() {
  const user = userEvent.setup();
  render(<App />);
  await user.click(screen.getByRole("button", { name: "Load fictional sample" }));
  await user.click(screen.getByRole("button", { name: "Confirm scenario" }));
  await screen.findByTestId("confirmed-note");
  await user.click(screen.getByRole("button", { name: "Optimize" }));
  await screen.findByTestId("optimize-result");
  return user;
}

async function addManual(user: ReturnType<typeof userEvent.setup>, o: { business: string; kind?: string; service?: string; value: string }) {
  await user.selectOptions(screen.getByLabelText("What-if business"), o.business);
  if (o.kind) await user.selectOptions(screen.getByLabelText("What-if change type"), o.kind);
  if (o.service) await user.selectOptions(screen.getByLabelText("What-if service"), o.service);
  const input = screen.getByLabelText("What-if new value (hours)");
  await user.clear(input);
  await user.type(input, o.value);
  await user.click(screen.getByRole("button", { name: "Add change" }));
}

const whatIfResult = () => screen.getByTestId("whatif-result");
const apply = (user: ReturnType<typeof userEvent.setup>) => user.click(screen.getByRole("button", { name: "Apply these changes" }));
const proposeText = async (user: ReturnType<typeof userEvent.setup>, text = "Make DesignCo design take 10 hours") => {
  await user.type(screen.getByLabelText("What would you like to try?"), text);
  await user.click(screen.getByRole("button", { name: "Propose changes" }));
};

describe("not ready", () => {
  it("is disabled until the scenario is confirmed and optimized", () => {
    stubBackend(() => envelope(500, "X"));
    render(<App />);
    expect(screen.getByTestId("whatif-not-ready")).toBeInTheDocument();
    expect(screen.getByLabelText("What would you like to try?")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Add change" })).toBeDisabled();
  });
});

describe("manual change: designco design = 10 h", () => {
  it("reviews first, sends apply=true with exactly the change, and shows no_exchange without an invented loop", async () => {
    const sims = stubBackend((b) => sim(designco600(b.scenario), noExchange, b.changes));
    const user = await optimized();
    await addManual(user, { business: "designco", kind: "estimate", service: "design", value: "10" });

    expect(screen.getByTestId("proposal")).toHaveTextContent("NOT applied yet");
    expect(screen.getByTestId("proposal-changes")).toHaveTextContent("DesignCo design estimate: 3 h → 10 h");
    expect(sims).toHaveLength(0); // nothing is sent before approval

    await apply(user);
    await screen.findByTestId("whatif-result");
    expect(sims).toHaveLength(1);
    expect(Object.keys(sims[0]).sort()).toEqual(["apply", "changes", "scenario"]); // no text
    expect(sims[0].apply).toBe(true);
    expect(sims[0].changes).toEqual(disruption.noExchange.changes);
    expect(sims[0].scenario.confirmed).toBe(true);
    // the base sent is the unmodified confirmed scenario
    expect(sims[0].scenario.businesses[1].estimates.find((e) => e.service === "design")!.minutes).toBe(180);

    const result = within(whatIfResult());
    expect(result.getByTestId("whatif-status")).toHaveTextContent("Feasible, but no beneficial exchange");
    expect(result.getByTestId("no-exchange-note")).toBeInTheDocument();
    expect(result.getByTestId("applied-changes")).toHaveTextContent("DesignCo design estimate: 3 h → 10 h");
    expect(result.getByRole("heading", { name: /What-if #1.*input version/ })).toBeInTheDocument();
    expect(result.getByTestId("certificate-status")).toBeInTheDocument();
    expect(screen.queryByTestId("proposal")).not.toBeInTheDocument();

    // original stays available and unchanged
    expect(screen.getByTestId("result-status")).toHaveTextContent("Beneficial exchange found");
    expect(screen.getByLabelText("Business 2 design estimate (hours)")).toHaveValue("3");
  });

  it("shows the real before/after of each assignment and the numbers side by side", async () => {
    stubBackend((b) => sim(designco600(b.scenario), noExchange, b.changes));
    const user = await optimized();
    await addManual(user, { business: "designco", kind: "estimate", service: "design", value: "10" });
    await apply(user);
    await screen.findByTestId("whatif-result");

    const diff = within(screen.getByTestId("assignment-diff"));
    const design = diff.getByRole("row", { name: /d-design/ });
    expect(design).toHaveAttribute("data-changed", "true");
    expect(design).toHaveTextContent("DesignCo, 180 min (3 h)");
    expect(design).toHaveTextContent("DevCo, 480 min (8 h)");
    const cmp = within(screen.getByTestId("comparison"));
    expect(cmp.getByRole("row", { name: /Production/ })).toHaveTextContent("540 min (9 h)");
    expect(cmp.getByRole("row", { name: /Production/ })).toHaveTextContent("1440 min (24 h)");
    expect(cmp.getByRole("row", { name: /Gross savings/ })).toHaveTextContent("900 min (15 h)");
    expect(cmp.getByRole("row", { name: /Gross savings/ })).toHaveTextContent("0 min");
  });

  it("a second what-if is labelled #2 and the first can be discarded without touching the base", async () => {
    stubBackend((b) => sim(designco600(b.scenario), noExchange, b.changes));
    const user = await optimized();
    await addManual(user, { business: "designco", kind: "estimate", service: "design", value: "10" });
    await apply(user);
    await screen.findByTestId("whatif-result");
    await user.clear(screen.getByLabelText("What-if new value (hours)"));
    await addManual(user, { business: "designco", kind: "estimate", service: "design", value: "10" });
    await apply(user);
    await waitFor(() => expect(within(whatIfResult()).getByRole("heading", { name: /What-if #2/ })).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Discard what-if" }));
    expect(screen.queryByTestId("whatif-result")).not.toBeInTheDocument();
    expect(screen.getByTestId("result-status")).toHaveTextContent("Beneficial exchange found");
  });
});

describe("manual change: all capacities zero", () => {
  it("shows infeasible with reasons, no figures, no graph, and original intact", async () => {
    const sims = stubBackend((b) => sim(allCapacityZero(b.scenario), infeasible, b.changes));
    const user = await optimized();
    for (const business of IDS) await addManual(user, { business, kind: "capacity", value: "0" });
    expect(screen.getByTestId("proposal-changes").querySelectorAll("li")).toHaveLength(3);
    await apply(user);
    await screen.findByTestId("whatif-result");

    expect(sims[0].changes).toEqual(disruption.infeasible.changes);
    const result = within(whatIfResult());
    expect(result.getByTestId("whatif-status")).toHaveTextContent("Infeasible");
    expect(result.getByTestId("whatif-reasons")).toHaveTextContent("CAPACITY_EXCEEDED");
    expect(result.queryByTestId("metrics")).not.toBeInTheDocument();
    expect(result.queryByTestId("network-graph")).not.toBeInTheDocument();
    expect(result.getByTestId("no-savings")).toBeInTheDocument();
    expect(result.getByTestId("certificate-none")).toBeInTheDocument();
    expect(within(result.getByTestId("comparison")).getByRole("row", { name: /Production/ })).toHaveTextContent("n/a (infeasible)");
    for (const row of within(result.getByTestId("assignment-diff")).getAllByRole("row").slice(1)) {
      expect(row).toHaveAttribute("data-changed", "true");
      expect(row).toHaveTextContent("none");
    }
    expect(screen.getByTestId("result-status")).toHaveTextContent("Beneficial exchange found");
  });
});

describe("text proposal then approval", () => {
  it("first sends text with apply=false, then sends the reviewed changes with apply=true and no text", async () => {
    const sims = stubBackend((b) =>
      b.apply ? sim(designco600(b.scenario), noExchange, b.changes) : sim(b.scenario, null, [DESIGN_CHANGE]),
    );
    const user = await optimized();
    await proposeText(user);
    await screen.findByTestId("proposal");

    expect(sims).toHaveLength(1);
    expect(Object.keys(sims[0]).sort()).toEqual(["apply", "changes", "scenario", "text"]);
    expect(sims[0]).toMatchObject({ apply: false, changes: [], text: "Make DesignCo design take 10 hours" });
    expect(screen.getByTestId("proposal")).toHaveTextContent("Proposed from your text “Make DesignCo design take 10 hours”");
    expect(screen.getByTestId("proposal")).toHaveTextContent("SIMULATED");
    expect(screen.getByTestId("proposal-changes")).toHaveTextContent("DesignCo design estimate: 3 h → 10 h");
    expect(screen.queryByTestId("whatif-result")).not.toBeInTheDocument(); // proposing never shows a result
    expect(screen.getByRole("button", { name: "Add change" })).toBeDisabled(); // cannot mix manual edits into a model proposal

    await apply(user);
    await screen.findByTestId("whatif-result");
    expect(sims).toHaveLength(2);
    expect(Object.keys(sims[1]).sort()).toEqual(["apply", "changes", "scenario"]);
    expect(sims[1]).toMatchObject({ apply: true, changes: [DESIGN_CHANGE] });
    expect(within(whatIfResult()).getByTestId("whatif-status")).toHaveTextContent("no beneficial exchange");
  });

  it("ignores a result that a proposal response tries to include", async () => {
    stubBackend((b) => sim(b.scenario, noExchange, [DESIGN_CHANGE]));
    const user = await optimized();
    await proposeText(user);
    await screen.findByTestId("proposal");
    expect(screen.queryByTestId("whatif-result")).not.toBeInTheDocument();
  });

  it("cancel preserves the base and the typed text and sends nothing further", async () => {
    const sims = stubBackend((b) => sim(b.scenario, null, [DESIGN_CHANGE]));
    const user = await optimized();
    await proposeText(user);
    await screen.findByTestId("proposal");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByTestId("proposal")).not.toBeInTheDocument();
    expect(screen.queryByTestId("whatif-result")).not.toBeInTheDocument();
    expect(sims).toHaveLength(1);
    expect(screen.getByTestId("result-status")).toHaveTextContent("Beneficial exchange found");
    expect(screen.getByLabelText("What would you like to try?")).toHaveValue("Make DesignCo design take 10 hours");
    expect(screen.getByTestId("confirmed-note")).toBeInTheDocument();
    expect(screen.getByLabelText("Business 2 design estimate (hours)")).toHaveValue("3");
  });

  it("an empty proposal is a notice, not an error and not an applicable change", async () => {
    stubBackend((b) => sim(b.scenario, null, []));
    const user = await optimized();
    await proposeText(user, "Do something vague");
    expect(await screen.findByText(/No changes were proposed/)).toBeInTheDocument();
    expect(screen.queryByTestId("proposal")).not.toBeInTheDocument();
  });

  it.each([
    ["an unknown business", [{ kind: "capacity", businessId: "ghost", minutes: 0 }], /unknown business "ghost"/],
    ["a kind outside the allowlist", [{ kind: "delete", businessId: "devco" }], /not an allowed change/],
    ["an extra field", [{ kind: "capacity", businessId: "devco", minutes: 0, path: "$.x" }], /not an allowed change/],
    ["a zero estimate", [{ kind: "estimate", businessId: "devco", service: "design", minutes: 0 }], /must be 1 to 1000000/],
    ["minutes over the bound", [{ kind: "capacity", businessId: "devco", minutes: 1000001 }], /must be 0 to 1000000/],
    ["a non-integer", [{ kind: "capacity", businessId: "devco", minutes: 1.5 }], /not an allowed change/],
    ["a duplicate target", [{ kind: "capacity", businessId: "devco", minutes: 1 }, { kind: "capacity", businessId: "devco", minutes: 2 }], /same field/],
    ["too many changes", Array.from({ length: 21 }, (_, i) => ({ kind: "capacity", businessId: "devco", minutes: i })), /At most 20/],
  ])("rejects a proposal with %s", async (_name, changes, message) => {
    stubBackend((b) => sim(b.scenario, null, changes));
    const user = await optimized();
    await proposeText(user);
    expect(await screen.findByTestId("whatif-error")).toHaveTextContent(message);
    expect(screen.getByTestId("whatif-error")).toHaveTextContent(/rejected/);
    expect(screen.queryByTestId("proposal")).not.toBeInTheDocument();
  });
});

describe("errors preserve user input", () => {
  it("a failed proposal keeps the description and shows the server error", async () => {
    stubBackend(() => envelope(502, "MODEL_FAILURE"));
    const user = await optimized();
    await proposeText(user);
    const error = await screen.findByTestId("whatif-error");
    expect(error).toHaveTextContent(/Proposal failed.*MODEL_FAILURE.*r-1/);
    expect(screen.getByLabelText("What would you like to try?")).toHaveValue("Make DesignCo design take 10 hours");
    expect(screen.queryByTestId("proposal")).not.toBeInTheDocument();
    expect(screen.queryByTestId("whatif-result")).not.toBeInTheDocument();
  });

  it("a failed apply keeps the reviewed changes, applies nothing, and a retry can succeed", async () => {
    stubBackend((b, call) => (call === 1 ? envelope(422, "VALIDATION_ERROR") : sim(designco600(b.scenario), noExchange, b.changes)));
    const user = await optimized();
    await addManual(user, { business: "designco", kind: "estimate", service: "design", value: "10" });
    await apply(user);
    expect(await screen.findByTestId("whatif-error")).toHaveTextContent(/Applying failed.*VALIDATION_ERROR.*nothing was applied/);
    expect(screen.getByTestId("proposal-changes")).toHaveTextContent("DesignCo design estimate: 3 h → 10 h");
    expect(screen.queryByTestId("whatif-result")).not.toBeInTheDocument();
    expect(screen.getByTestId("result-status")).toHaveTextContent("Beneficial exchange found");
    await apply(user);
    await screen.findByTestId("whatif-result");
  });

  it("a network failure is shown without any substitute result", async () => {
    stubBackend(() => {
      throw new TypeError("Failed to fetch");
    });
    const user = await optimized();
    await addManual(user, { business: "devco", kind: "capacity", value: "0" });
    await apply(user);
    expect(await screen.findByTestId("whatif-error")).toHaveTextContent(/Cannot reach the backend/);
    expect(screen.queryByTestId("whatif-result")).not.toBeInTheDocument();
  });

  it("discards a response in the wrong mode", async () => {
    stubBackend((b) => sim(designco600(b.scenario), noExchange, b.changes, "live"));
    const user = await optimized();
    await addManual(user, { business: "designco", kind: "estimate", service: "design", value: "10" });
    await apply(user);
    expect(await screen.findByTestId("whatif-error")).toHaveTextContent(/Result discarded/);
    expect(screen.queryByTestId("whatif-result")).not.toBeInTheDocument();
  });

  it("discards an applied response whose scenario goes beyond the approved changes", async () => {
    stubBackend((b) => {
      const tampered = designco600(b.scenario);
      tampered.businesses[0].capacityMinutes = 1; // not approved
      return sim(tampered, noExchange, b.changes);
    });
    const user = await optimized();
    await addManual(user, { business: "designco", kind: "estimate", service: "design", value: "10" });
    await apply(user);
    expect(await screen.findByTestId("whatif-error")).toHaveTextContent(/does not match your approved changes/);
    expect(screen.queryByTestId("whatif-result")).not.toBeInTheDocument();
  });

  it("discards an applied response without a result", async () => {
    stubBackend((b) => sim(designco600(b.scenario), null, b.changes));
    const user = await optimized();
    await addManual(user, { business: "designco", kind: "estimate", service: "design", value: "10" });
    await apply(user);
    expect(await screen.findByTestId("whatif-error")).toHaveTextContent(/returned no result/);
    expect(screen.queryByTestId("whatif-result")).not.toBeInTheDocument();
  });
});

describe("stale responses are ignored", () => {
  it("cancelling a pending proposal drops its late response", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    stubBackend(async (b) => {
      await gate;
      return sim(b.scenario, null, [DESIGN_CHANGE]);
    });
    const user = await optimized();
    await proposeText(user);
    await screen.findByText("Proposing…");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    release();
    await new Promise((r) => setTimeout(r, 30));
    expect(screen.queryByTestId("proposal")).not.toBeInTheDocument();
    expect(screen.getByLabelText("What would you like to try?")).toBeEnabled();
  });

  it("editing the inputs while applying drops the late result and the proposal", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    stubBackend(async (b) => {
      await gate;
      return sim(designco600(b.scenario), noExchange, b.changes);
    });
    const user = await optimized();
    await addManual(user, { business: "designco", kind: "estimate", service: "design", value: "10" });
    await apply(user);
    await screen.findByText("Applying…");
    await user.type(screen.getByLabelText("Business 1 name"), "x");
    release();
    await new Promise((r) => setTimeout(r, 30));
    expect(screen.queryByTestId("whatif-result")).not.toBeInTheDocument();
    expect(screen.queryByTestId("proposal")).not.toBeInTheDocument();
    expect(screen.queryByTestId("optimize-result")).not.toBeInTheDocument();
    expect(screen.getByTestId("whatif-not-ready")).toBeInTheDocument();
  });

  it("editing the inputs removes an applied what-if", async () => {
    stubBackend((b) => sim(designco600(b.scenario), noExchange, b.changes));
    const user = await optimized();
    await addManual(user, { business: "designco", kind: "estimate", service: "design", value: "10" });
    await apply(user);
    await screen.findByTestId("whatif-result");
    await user.type(screen.getByLabelText("Business 1 name"), "x");
    expect(screen.queryByTestId("whatif-result")).not.toBeInTheDocument();
  });
});

describe("manual input validation", () => {
  it.each([
    ["sub-minute precision", { business: "devco", kind: "capacity", value: "0.001" }, /sub-minute precision/],
    ["a zero estimate", { business: "devco", kind: "estimate", service: "coding", value: "0" }, /at least 1 minute/],
    ["text", { business: "devco", kind: "capacity", value: "lots" }, /Enter a non-negative number/],
    ["no business", { business: "", kind: "capacity", value: "1" }, /Choose a business/],
  ])("rejects %s without sending or proposing anything", async (_name, o, message) => {
    const sims = stubBackend(() => envelope(500, "X"));
    const user = await optimized();
    await addManual(user, o);
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.queryByTestId("proposal")).not.toBeInTheDocument();
    expect(sims).toHaveLength(0);
  });

  it("rejects changing the same field twice and keeps the first change", async () => {
    stubBackend(() => envelope(500, "X"));
    const user = await optimized();
    await addManual(user, { business: "devco", kind: "capacity", value: "1" });
    await addManual(user, { business: "devco", kind: "capacity", value: "2" });
    expect(await screen.findByRole("alert")).toHaveTextContent(/same field/);
    expect(screen.getByTestId("proposal-changes").querySelectorAll("li")).toHaveLength(1);
  });

  it("a change can be removed before applying", async () => {
    stubBackend(() => envelope(500, "X"));
    const user = await optimized();
    await addManual(user, { business: "devco", kind: "capacity", value: "1" });
    await user.click(screen.getByRole("button", { name: "Remove change 1" }));
    expect(screen.queryByTestId("proposal")).not.toBeInTheDocument();
  });

  it("an empty description is refused locally", async () => {
    const sims = stubBackend(() => envelope(500, "X"));
    const user = await optimized();
    await user.click(screen.getByRole("button", { name: "Propose changes" }));
    expect(await screen.findByTestId("whatif-error")).toHaveTextContent(/Describe the change/);
    expect(sims).toHaveLength(0);
  });
});
