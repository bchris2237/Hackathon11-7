import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { readConfig } from "./config";

// /health is retried twice on transient failures (250 ms + 500 ms) before the error is shown.
const LONG = { timeout: 4000 };

function mockFetch(impl: () => Promise<Response>) {
  vi.stubGlobal("fetch", vi.fn(impl));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("App shell", () => {
  it("always shows the fixture-mode banner by default", async () => {
    mockFetch(() => new Promise(() => {})); // never resolves
    render(<App />);
    expect(screen.getByTestId("mode-banner")).toHaveTextContent(/FIXTURE MODE/);
    expect(screen.getByText(/Checking \/health/)).toBeInTheDocument();
  });

  it("shows backend details when /health matches the contract", async () => {
    mockFetch(async () =>
      Response.json({ status: "ok", schemaVersion: "1", commit: "abc1234", mode: "fixture" }),
    );
    render(<App />);
    expect(await screen.findByText(/Backend OK/)).toBeInTheDocument();
    expect(screen.getByText("abc1234")).toBeInTheDocument();
  });

  it("reports the backend as unavailable instead of inventing a status", async () => {
    mockFetch(async () => {
      throw new TypeError("Failed to fetch");
    });
    render(<App />);
    expect(await screen.findByRole("alert", {}, LONG)).toHaveTextContent(/Backend unavailable/);
    expect(screen.queryByText(/Backend OK/)).not.toBeInTheDocument();
  });

  it("rejects a /health body that does not match the v1 contract", async () => {
    mockFetch(async () => Response.json({ status: "ok" }));
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/does not match the v1 contract/);
  });

  it("reports a non-200 /health as unavailable", async () => {
    mockFetch(async () => new Response("nope", { status: 503 }));
    render(<App />);
    expect(await screen.findByRole("alert", {}, LONG)).toHaveTextContent(/HTTP 503/);
  });

  it("lists the features that are not built yet", () => {
    mockFetch(() => new Promise(() => {}));
    render(<App />);
    expect(screen.getByText("Not built yet")).toBeInTheDocument();
  });
});

describe("App live mode", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("shows the live banner, no SIMULATED label, and a live failure without substitution", async () => {
    vi.stubEnv("VITE_DATA_MODE", "live");
    mockFetch(async () => {
      throw new TypeError("Failed to fetch");
    });
    render(<App />);
    expect(screen.getByTestId("mode-banner")).toHaveTextContent(/LIVE MODE/);
    expect(screen.getByTestId("mode-banner")).not.toHaveTextContent(/SIMULATED/);
    expect(await screen.findByRole("alert", {}, LONG)).toHaveTextContent(/Backend unavailable/);
    expect(screen.queryByText(/Backend OK/)).not.toBeInTheDocument();
  });

  it("flags a backend whose mode differs from the configured mode", async () => {
    vi.stubEnv("VITE_DATA_MODE", "live");
    mockFetch(async () => Response.json({ status: "ok", schemaVersion: "1", commit: "abc", mode: "fixture" }));
    render(<App />);
    expect(await screen.findByText(/Mode mismatch/)).toBeInTheDocument();
  });

  it("fixture banner says SIMULATED and a skip link plus both panels exist", () => {
    mockFetch(() => new Promise(() => {}));
    render(<App />);
    expect(screen.getByTestId("mode-banner")).toHaveTextContent(/SIMULATED/);
    expect(screen.getByRole("link", { name: /skip to workspace/i })).toHaveAttribute("href", "#workspace");
    expect(screen.getByRole("heading", { name: "Businesses and scenario" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Results and backend" })).toBeInTheDocument();
  });
});

describe("mode mismatch", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("tells the user what to do and one click on the button resolves it", async () => {
    vi.stubEnv("VITE_DATA_MODE", "live");
    mockFetch(async () => Response.json({ status: "ok", schemaVersion: "1", commit: "abc", mode: "fixture" }));
    render(<App />);
    const alert = await screen.findByTestId("mode-mismatch");
    expect(alert).toHaveTextContent("this app is in live mode but the backend reports fixture mode");
    expect(alert).toHaveTextContent("APP_MODE=live");
    expect(screen.getByTestId("mode-banner")).toHaveTextContent(/LIVE MODE/);

    await userEvent.click(within(alert).getByRole("button", { name: "Switch the app to fixture mode" }));
    expect(screen.getByTestId("mode-banner")).toHaveTextContent(/FIXTURE MODE/);
    await waitFor(() => expect(screen.queryByTestId("mode-mismatch")).not.toBeInTheDocument());
    expect(await screen.findByText(/Backend OK/)).toBeInTheDocument();
  });

  it("is not shown when the app and the backend agree", async () => {
    mockFetch(async () => Response.json({ status: "ok", schemaVersion: "1", commit: "abc", mode: "fixture" }));
    render(<App />);
    await screen.findByText(/Backend OK/);
    expect(screen.queryByTestId("mode-mismatch")).not.toBeInTheDocument();
  });
});

describe("readConfig", () => {
  it("defaults to fixture mode and the local backend", () => {
    expect(readConfig({})).toEqual({ apiBaseUrl: "http://127.0.0.1:8000", dataMode: "fixture", configError: null });
  });

  it("trims trailing slashes and accepts live", () => {
    const config = readConfig({ VITE_API_BASE_URL: "https://api.example.test///", VITE_DATA_MODE: "live" });
    expect(config).toEqual({ apiBaseUrl: "https://api.example.test", dataMode: "live", configError: null });
  });

  it("flags an invalid mode instead of silently picking one", () => {
    expect(readConfig({ VITE_DATA_MODE: "demo" }).configError).toMatch(/must be "fixture" or "live"/);
  });
});
