import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { readConfig } from "./config";

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
    expect(await screen.findByRole("alert")).toHaveTextContent(/Backend unavailable/);
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
    expect(await screen.findByRole("alert")).toHaveTextContent(/HTTP 503/);
  });

  it("lists the features that are not built yet", () => {
    mockFetch(() => new Promise(() => {}));
    render(<App />);
    expect(screen.getByText("Not built yet")).toBeInTheDocument();
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
