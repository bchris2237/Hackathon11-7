// Keyboard focus rules (C8) checked in jsdom: focus must land somewhere sensible after an error
// or a change, and must never be dropped because a pressed button became disabled or vanished.
// Results are hand-written TEST INPUTS behind a stubbed fetch, not solver output.
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { ApiNetworkError } from "../lib/api";
import { AsyncError } from "./AsyncState";
import ScrollRegion from "./ScrollRegion";

afterEach(() => vi.unstubAllGlobals());

const cert = {
  schemaVersion: "1",
  scenarioDigest: "a".repeat(64),
  allocationDigest: "b".repeat(64),
  verification: "valid",
  assumptions: ["Estimates are supplied by the user."],
  checks: [{ name: "within capacity", passed: true }],
};
const metrics = (p: number) =>
  ["devco", "designco", "copyco"].map((businessId) => ({
    businessId,
    baselineMinutes: 480,
    productionMinutes: p,
    coordinationMinutes: 0,
    effortMinutes: p,
    netSavingsMinutes: 480 - p,
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
  ...collaboration,
  status: "no_exchange",
  assignments: collaboration.assignments.map((a, i) => ({ ...a, providerId: ["devco", "designco", "copyco"][i], minutes: 480 })),
  businessMetrics: metrics(480),
  totals: { baselineMinutes: 1440, productionMinutes: 1440, coordinationMinutes: 0, effortMinutes: 1440, grossSavingsMinutes: 0, netSavingsMinutes: 0 },
  reasons: [{ code: "NO_BENEFICIAL_EXCHANGE", message: "No exchange improves on self-production." }],
};
const DESIGN = { kind: "estimate", businessId: "designco", service: "design", minutes: 600 };

function stub(opts: { optimize?: () => Response | Promise<Response> } = {}) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const path = new URL(url).pathname;
      if (path === "/health") return Response.json({ status: "ok", schemaVersion: "1", commit: "abc", mode: "fixture" });
      const body = JSON.parse(init.body as string);
      if (path === "/confirm") return Response.json({ scenario: body.scenario });
      if (path === "/optimize") return opts.optimize ? opts.optimize() : Response.json({ result: collaboration, mode: "fixture" });
      if (path === "/simulate") {
        if (!body.apply) return Response.json({ proposedChanges: [DESIGN], requiresConfirmation: true, scenario: body.scenario, result: null, mode: "fixture" });
        const scenario = structuredClone(body.scenario);
        scenario.businesses[1].estimates.find((e: any) => e.service === "design").minutes = 600;
        return Response.json({ proposedChanges: body.changes, requiresConfirmation: false, scenario, result: noExchange, mode: "fixture" });
      }
      throw new TypeError("no route " + path);
    }),
  );
}

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

describe("AsyncError focus", () => {
  it("takes focus when it appears, so the user lands on what went wrong", () => {
    render(<AsyncError error={new ApiNetworkError("Cannot reach it")} onRetry={() => {}} />);
    expect(screen.getByRole("alert")).toHaveFocus();
  });

  it("does not take focus when told not to (an error nobody asked for)", () => {
    render(<AsyncError error={new ApiNetworkError("x")} focusOnMount={false} />);
    expect(screen.getByRole("alert")).not.toHaveFocus();
  });

  it("takes focus again for a new error but not on a plain re-render", () => {
    const first = new ApiNetworkError("one");
    const { rerender } = render(<AsyncError error={first} />);
    const box = screen.getByRole("alert");
    box.blur();
    rerender(<AsyncError error={first} />);
    expect(box).not.toHaveFocus();
    rerender(<AsyncError error={new ApiNetworkError("two")} />);
    expect(screen.getByRole("alert")).toHaveFocus();
  });
});

describe("a pressed button keeps focus while its request runs", () => {
  it("Optimize stays focused and aria-disabled (not removed from the tab order) while pending", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    stub({
      optimize: async () => {
        await gate;
        return Response.json({ result: collaboration, mode: "fixture" });
      },
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("button", { name: "Load fictional sample" }));
    await user.click(screen.getByRole("button", { name: "Confirm scenario" }));
    await screen.findByTestId("confirmed-note");
    await user.click(screen.getByRole("button", { name: "Optimize" }));
    const pending = await screen.findByRole("button", { name: "Optimizing…" });
    expect(pending).toHaveFocus();
    expect(pending).toHaveAttribute("aria-disabled", "true");
    expect(pending).not.toBeDisabled();
    release();
    await screen.findByTestId("optimize-result");
    expect(screen.getByRole("button", { name: "Optimize" })).toHaveFocus();
  });

  it("announces the finished result in a live region without moving focus", async () => {
    stub();
    await optimized();
    expect(screen.getByTestId("optimize-announce")).toHaveTextContent("Optimization finished. Beneficial exchange found.");
    expect(screen.getByRole("button", { name: "Optimize" })).toHaveFocus();
  });
});

describe("focus after errors", () => {
  it("moves to the error box after a failed optimize and keeps the inputs", async () => {
    stub({
      optimize: () =>
        Response.json({ error: { code: "UNAVAILABLE", message: "down", fields: [], requestId: "r-1" } }, { status: 503 }),
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("button", { name: "Load fictional sample" }));
    await user.click(screen.getByRole("button", { name: "Confirm scenario" }));
    await screen.findByTestId("confirmed-note");
    await user.click(screen.getByRole("button", { name: "Optimize" }));
    expect(await screen.findByTestId("optimize-error")).toHaveFocus();
    expect(screen.getByLabelText("Business 1 name")).toHaveValue("DevCo");
    await user.tab();
    expect(screen.getByRole("button", { name: "Try again" })).toHaveFocus();
  });

  it("the health check failing on load does not steal focus", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "ok" }))); // not the contract: no retry
    render(<App />);
    const alert = await screen.findByTestId("health-error");
    expect(alert).not.toHaveFocus();
  });

  it("a bad manual value focuses the value field and describes the problem", async () => {
    stub();
    const user = await optimized();
    await user.selectOptions(screen.getByLabelText("What-if business"), "devco");
    await user.selectOptions(screen.getByLabelText("What-if change type"), "capacity");
    fireEvent.change(screen.getByLabelText("What-if new value (hours)"), { target: { value: "0.001" } });
    await user.click(screen.getByRole("button", { name: "Add change" }));
    const field = screen.getByLabelText("What-if new value (hours)");
    expect(field).toHaveFocus();
    expect(field).toHaveAttribute("aria-invalid", "true");
    const error = document.getElementById(field.getAttribute("aria-describedby")!);
    expect(error).toHaveTextContent(/sub-minute precision/);
  });

  it("a missing business focuses the business select", async () => {
    stub();
    const user = await optimized();
    fireEvent.change(screen.getByLabelText("What-if new value (hours)"), { target: { value: "1" } });
    await user.click(screen.getByRole("button", { name: "Add change" }));
    expect(screen.getByLabelText("What-if business")).toHaveFocus();
    expect(screen.getByLabelText("What-if business")).toHaveAttribute("aria-invalid", "true");
  });
});

describe("focus is not lost when the pressed element goes away", () => {
  async function withTextProposal() {
    stub();
    const user = await optimized();
    await user.type(screen.getByLabelText("What would you like to try?"), "Make DesignCo design take 10 hours");
    await user.click(screen.getByRole("button", { name: "Propose changes" }));
    await screen.findByTestId("proposal");
    return user;
  }

  it("a model proposal moves focus to its review heading", async () => {
    await withTextProposal();
    expect(screen.getByRole("heading", { name: /Review these changes/ })).toHaveFocus();
  });

  it("Cancel moves focus to the section heading", async () => {
    const user = await withTextProposal();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByTestId("proposal")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "What-if simulation" })).toHaveFocus();
  });

  it("Apply moves focus to the what-if result, and Discard back to the section heading", async () => {
    const user = await withTextProposal();
    await user.click(screen.getByRole("button", { name: "Apply these changes" }));
    await screen.findByTestId("whatif-result");
    expect(screen.getByRole("heading", { name: /^What-if #1/ })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Discard what-if" }));
    expect(screen.getByRole("heading", { name: "What-if simulation" })).toHaveFocus();
  });

  it("removing the last manual change moves focus to Add change", async () => {
    stub();
    const user = await optimized();
    await user.selectOptions(screen.getByLabelText("What-if business"), "devco");
    await user.selectOptions(screen.getByLabelText("What-if change type"), "capacity");
    fireEvent.change(screen.getByLabelText("What-if new value (hours)"), { target: { value: "5" } });
    await user.click(screen.getByRole("button", { name: "Add change" }));
    await user.click(screen.getByRole("button", { name: "Remove change 1" }));
    expect(screen.getByRole("button", { name: "Add change" })).toHaveFocus();
  });
});

describe("ScrollRegion", () => {
  const sizes = (scrollWidth: number, clientWidth: number) => {
    vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(scrollWidth);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(clientWidth);
  };
  afterEach(() => vi.restoreAllMocks());

  it("becomes a labelled, focusable group only when its content overflows", () => {
    sizes(600, 300);
    render(
      <ScrollRegion label="Wide table">
        <table />
      </ScrollRegion>,
    );
    const group = screen.getByRole("group", { name: "Wide table (scrolls sideways)" });
    expect(group).toHaveAttribute("tabindex", "0");
    expect(screen.getByTestId("scroll-hint")).toHaveTextContent("Scroll sideways");
  });

  it("adds no tab stop when everything fits", () => {
    sizes(300, 300);
    const { container } = render(
      <ScrollRegion label="Narrow table">
        <table />
      </ScrollRegion>,
    );
    expect(container.firstElementChild).not.toHaveAttribute("tabindex");
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    expect(screen.queryByTestId("scroll-hint")).not.toBeInTheDocument();
  });
});
