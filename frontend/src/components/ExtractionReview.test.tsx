import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Ajv2020 from "ajv/dist/2020.js";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { isExtractResponse } from "../lib/api";

const root = resolve(__dirname, "../../..");
const readJson = (rel: string): any => JSON.parse(readFileSync(resolve(root, rel), "utf8"));
const extraction = readJson("fixtures/extraction.json");
const HEALTH = { status: "ok", schemaVersion: "1", commit: "abc", mode: "fixture" };

type Handler = (body: any, init: RequestInit) => Response | Promise<Response>;
function stubBackend(routes: Record<string, Handler>) {
  const calls: { path: string; body: any; init: RequestInit }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const path = new URL(url).pathname;
      if (path === "/health") return Response.json(HEALTH);
      const body = init.body ? JSON.parse(init.body as string) : undefined;
      calls.push({ path, body, init });
      const handler = routes[path];
      if (!handler) throw new TypeError("no route " + path);
      return handler(body, init);
    }),
  );
  return calls;
}
afterEach(() => vi.unstubAllGlobals());

const envelope = (status: number, code: string, message: string) =>
  Response.json({ error: { code, message, fields: [], requestId: "req-1" } }, { status });

/** Loads the sample, then replaces each description with the fixture request text. */
async function prepare() {
  const user = userEvent.setup();
  render(<App />);
  await user.click(screen.getByRole("button", { name: "Load fictional sample" }));
  extraction.request.descriptions.forEach((d: any, i: number) => {
    fireEvent.change(screen.getByLabelText(`Business ${i + 1} description`), { target: { value: d.text } });
  });
  return user;
}

describe("extraction preview", () => {
  it("shows original text, proposed fields, quotes and highlights missing values (fixture)", async () => {
    const calls = stubBackend({ "/extract": () => Response.json(extraction.response) });
    const user = await prepare();
    await user.click(screen.getByRole("button", { name: "Extract draft" }));

    const review = await screen.findByTestId("extraction-review");
    expect(within(review).getByText(/SIMULATED \(fixture\) extraction/)).toBeInTheDocument();
    expect(within(review).getByText(/CopyCo writes marketing copy/)).toBeInTheDocument();
    expect(within(review).getByText("Coding takes us 3 hours per deliverable")).toBeInTheDocument();
    expect(within(review).getAllByTestId("missing-badge")).toHaveLength(1);
    expect(within(review).getByTestId("missing-fields")).toHaveTextContent("businesses[2].coordinationMinutes");

    // The request carried only id/name/text, as the contract says.
    expect(calls[0].body.descriptions).toEqual(extraction.request.descriptions);

    // Null stays blank in the form (never 0) and blocks confirmation.
    expect(screen.getByLabelText("Business 3 coordination (hours)")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Confirm scenario" })).toBeDisabled();
    expect(screen.getByTestId("confirm-blockers")).toHaveTextContent("businesses[2].coordination");
    expect(screen.queryByTestId("confirmed-note")).not.toBeInTheDocument();
  });

  it("after the user completes the value, /confirm receives a contract-valid scenario", async () => {
    const ajv = new Ajv2020({ strict: true, strictTypes: false });
    ajv.addSchema(readJson("contracts/scenario.schema.json"));
    const validate = ajv.getSchema("https://trademind.example/contracts/scenario.schema.json")!;
    expect(validate).toBeTruthy();

    const calls = stubBackend({
      "/extract": () => Response.json(extraction.response),
      "/confirm": (body) => Response.json({ scenario: body.scenario }),
    });
    const user = await prepare();
    await user.click(screen.getByRole("button", { name: "Extract draft" }));
    await screen.findByTestId("extraction-review");

    await user.type(screen.getByLabelText("Business 3 coordination (hours)"), "1");
    await user.click(screen.getByRole("button", { name: "Confirm scenario" }));
    expect(await screen.findByTestId("confirmed-note")).toBeInTheDocument();

    const sent = calls.find((c) => c.path === "/confirm")!;
    expect(Object.keys(sent.body)).toEqual(["scenario"]);
    expect(validate(sent.body.scenario), JSON.stringify(validate.errors)).toBe(true);
    expect(sent.body.scenario.confirmed).toBe(true);
    expect(sent.body.scenario.businesses[2].coordinationMinutes).toBe(60);
    expect(sent.body.scenario.businesses[0].capacityMinutes).toBe(600);
    // Only a JSON content type is sent: no credentials or auth headers from the browser.
    expect(Object.keys(sent.init.headers as Record<string, string>)).toEqual(["Content-Type"]);

    // Any edit withdraws the confirmation.
    await user.type(screen.getByLabelText("Business 1 name"), "x");
    expect(screen.queryByTestId("confirmed-note")).not.toBeInTheDocument();
  });

  it("shows a server failure on extract without touching the user's input", async () => {
    stubBackend({ "/extract": () => envelope(502, "MODEL_FAILURE", "The model call failed.") });
    const user = await prepare();
    await user.click(screen.getByRole("button", { name: "Extract draft" }));
    const alert = await screen.findByText(/Extraction failed/);
    expect(alert).toHaveTextContent(/MODEL_FAILURE/);
    expect(alert).toHaveTextContent(/request req-1/);
    expect(screen.queryByTestId("extraction-review")).not.toBeInTheDocument(); // no substituted draft
    expect(screen.getByLabelText("Business 1 name")).toHaveValue("DevCo");
    expect(screen.getByLabelText("Business 1 description")).toHaveValue(extraction.request.descriptions[0].text);
  });

  it("shows a network failure on extract with no fallback", async () => {
    stubBackend({
      "/extract": () => {
        throw new TypeError("Failed to fetch");
      },
    });
    const user = await prepare();
    await user.click(screen.getByRole("button", { name: "Extract draft" }));
    expect(await screen.findByText(/Cannot reach the backend/)).toBeInTheDocument();
    expect(screen.queryByTestId("extraction-review")).not.toBeInTheDocument();
  });

  it("refuses to call /extract with no usable description", async () => {
    const calls = stubBackend({ "/extract": () => Response.json(extraction.response) });
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("button", { name: "Extract draft" }));
    expect(await screen.findByText(/at least one business/)).toBeInTheDocument();
    expect(calls).toHaveLength(0);
  });

  it("ignores a late extraction response after the user edited the input", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    stubBackend({
      "/extract": async () => {
        await gate;
        return Response.json(extraction.response);
      },
    });
    const user = await prepare();
    await user.click(screen.getByRole("button", { name: "Extract draft" }));
    await screen.findByText("Extracting…");
    await user.type(screen.getByLabelText("Business 1 name"), "X");
    release();
    await waitFor(() => expect(screen.getByRole("button", { name: "Extract draft" })).toBeEnabled());
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByTestId("extraction-review")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Business 1 name")).toHaveValue("DevCoX");
    expect(screen.getByLabelText("Business 3 coordination (hours)")).toHaveValue("0"); // not overwritten by the draft's null
  });
});

describe("confirmation", () => {
  it("a missing owner baseline blocks confirm and nothing is sent", async () => {
    const calls = stubBackend({ "/confirm": (b) => Response.json({ scenario: b.scenario }) });
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("button", { name: "Load fictional sample" }));
    await user.clear(screen.getByLabelText("Business 1 design estimate (hours)"));
    const button = screen.getByRole("button", { name: "Confirm scenario" });
    expect(button).toBeDisabled();
    expect(screen.getByTestId("confirm-blockers")).toHaveTextContent(/baseline/);
    fireEvent.click(button);
    expect(calls).toHaveLength(0);
  });

  it("shows a 422 from /confirm and preserves edits and no confirmed state", async () => {
    stubBackend({ "/confirm": () => envelope(422, "MISSING_BASELINE", "Owner estimate missing.") });
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("button", { name: "Load fictional sample" }));
    await user.type(screen.getByLabelText("Business 2 name"), "!");
    await user.click(screen.getByRole("button", { name: "Confirm scenario" }));
    expect(await screen.findByText(/Confirmation failed/)).toHaveTextContent(/MISSING_BASELINE/);
    expect(screen.getByLabelText("Business 2 name")).toHaveValue("DesignCo!");
    expect(screen.queryByTestId("confirmed-note")).not.toBeInTheDocument();
  });

  it("does not mark confirmed when /confirm returns a malformed body", async () => {
    stubBackend({ "/confirm": () => Response.json({ ok: true }) });
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("button", { name: "Load fictional sample" }));
    await user.click(screen.getByRole("button", { name: "Confirm scenario" }));
    expect(await screen.findByText(/does not match the v1 contract/)).toBeInTheDocument();
    expect(screen.queryByTestId("confirmed-note")).not.toBeInTheDocument();
  });
});

describe("contract guards", () => {
  it("accepts the fixture extraction response and rejects a draft that claims confirmed", () => {
    expect(isExtractResponse(extraction.response)).toBe(true);
    const bad = structuredClone(extraction.response);
    bad.draft.confirmed = true;
    expect(isExtractResponse(bad)).toBe(false);
    expect(isExtractResponse({ draft: {} })).toBe(false);
  });

  it("the frontend package carries no AWS SDK", () => {
    const pkg = readFileSync(resolve(root, "frontend/package.json"), "utf8");
    expect(pkg).not.toMatch(/aws-sdk|@aws-sdk|amazon/i);
  });
});
