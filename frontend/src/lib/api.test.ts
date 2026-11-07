import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError, ApiUnavailableError, ModeMismatchError, describeError } from "./api";
import type { Scenario } from "../../../contracts/models";
import canonical from "../../../fixtures/canonical.json";

const scenario = canonical as unknown as Scenario;
const client = (mode: "live" | "fixture" = "live", timeoutMs?: number) =>
  new ApiClient({ apiBaseUrl: "http://api.test/", dataMode: mode, timeoutMs });

afterEach(() => vi.unstubAllGlobals());
const stubFetch = (impl: (url: string, init?: RequestInit) => Promise<Response>) =>
  vi.stubGlobal("fetch", vi.fn(impl));

describe("ApiClient", () => {
  it("posts JSON to the right URL and returns a matching-mode body", async () => {
    stubFetch(async () => Response.json({ result: {}, mode: "live" }));
    const res = await client().optimize({ scenario });
    expect(res.mode).toBe("live");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("http://api.test/optimize");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body).scenario.schemaVersion).toBe("1");
  });

  it("maps the v1 error envelope to ApiError with request id", async () => {
    stubFetch(async () =>
      Response.json(
        { error: { code: "MISSING_BASELINE", message: "No baseline", fields: ["businesses[0]"], requestId: "r-1" } },
        { status: 422 },
      ),
    );
    const error = await client().optimize({ scenario }).catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(422);
    expect(describeError(error)).toContain("MISSING_BASELINE");
    expect(describeError(error)).toContain("r-1");
  });

  it("never falls back: a network failure is an ApiUnavailableError", async () => {
    stubFetch(async () => {
      throw new TypeError("Failed to fetch");
    });
    await expect(client().optimize({ scenario })).rejects.toBeInstanceOf(ApiUnavailableError);
  });

  it("rejects a response whose mode differs from the configured mode", async () => {
    stubFetch(async () => Response.json({ result: {}, mode: "fixture" }));
    await expect(client("live").optimize({ scenario })).rejects.toBeInstanceOf(ModeMismatchError);
  });

  it("rejects a body without a valid mode", async () => {
    stubFetch(async () => Response.json({ result: {} }));
    await expect(client().optimize({ scenario })).rejects.toBeInstanceOf(ApiUnavailableError);
  });

  it("times out with a readable message", async () => {
    stubFetch(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("t", "TimeoutError")));
        }),
    );
    await expect(client("live", 20).health()).rejects.toThrow(/did not answer within/);
  });

  it("a caller abort surfaces as AbortError, not as an error message", async () => {
    stubFetch(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("a", "AbortError")));
        }),
    );
    const controller = new AbortController();
    const pending = client().health({ signal: controller.signal });
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });

  it("rejects a /health body outside the v1 contract", async () => {
    stubFetch(async () => Response.json({ status: "ok" }));
    await expect(client().health()).rejects.toThrow(/does not match the v1 contract/);
  });
});
