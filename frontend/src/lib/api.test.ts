import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiClient,
  ApiError,
  ApiHttpError,
  ApiNetworkError,
  ApiTimeoutError,
  ApiUnavailableError,
  ModeMismatchError,
  describeError,
  isTransient,
  parseRetryAfter,
  retryAfterOf,
} from "./api";
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
    stubFetch(async () => Response.json({ text: "hi", mode: "live" }));
    const res = await client().explain({ scenario, result: {} as never });
    expect(res.mode).toBe("live");
    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("http://api.test/explain");
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

// ---- C7: Retry-After, error classes, bounded automatic retries ----------------------------

const HEALTH_OK = { status: "ok", schemaVersion: "1", commit: "abc", mode: "live" };
const errorBody = (status: number, code: string, headers: Record<string, string> = {}) =>
  Response.json({ error: { code, message: "Failed on purpose.", fields: [], requestId: "req-7" } }, { status, headers });

function retryingClient(overrides: { healthRetries?: number; maxRetryWaitMs?: number } = {}) {
  const waits: number[] = [];
  const instance = new ApiClient({
    apiBaseUrl: "http://api.test",
    dataMode: "live",
    sleep: async (ms) => {
      waits.push(ms);
    },
    ...overrides,
  });
  return { instance, waits };
}
const fetchCalls = () => (fetch as ReturnType<typeof vi.fn>).mock.calls.length;

describe("parseRetryAfter", () => {
  it.each([
    ["0", 0],
    ["5", 5],
    [" 120 ", 120],
  ])("delta-seconds %j -> %i", (header, seconds) => {
    expect(parseRetryAfter(header)).toBe(seconds);
  });

  it("converts an HTTP date relative to now and never goes negative", () => {
    const now = Date.parse("Wed, 21 Oct 2026 07:28:00 GMT");
    expect(parseRetryAfter("Wed, 21 Oct 2026 07:28:30 GMT", now)).toBe(30);
    expect(parseRetryAfter("Wed, 21 Oct 2026 07:27:00 GMT", now)).toBe(0);
  });

  it.each([null, undefined, "", "soon", "-5", "1.5", "12abc"])("ignores %j", (header) => {
    expect(parseRetryAfter(header)).toBeUndefined();
  });
});

describe("error classes", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("a timeout and a disconnect are different, both still ApiUnavailableError", async () => {
    stubFetch(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("t", "TimeoutError")));
        }),
    );
    const timeout = await client("live", 20).health().catch((e) => e);
    expect(timeout).toBeInstanceOf(ApiTimeoutError);
    expect(timeout).toBeInstanceOf(ApiUnavailableError);

    stubFetch(async () => {
      throw new TypeError("Failed to fetch");
    });
    const { instance } = retryingClient({ healthRetries: 0 });
    const down = await instance.health().catch((e) => e);
    expect(down).toBeInstanceOf(ApiNetworkError);
    expect(down).toBeInstanceOf(ApiUnavailableError);
  });

  it("keeps Retry-After on a structured error and on an unstructured HTTP error", async () => {
    stubFetch(async () => errorBody(429, "THROTTLED", { "Retry-After": "7" }));
    const { instance } = retryingClient({ healthRetries: 0 });
    const structured = await instance.optimize({ scenario }).catch((e) => e);
    expect(structured).toBeInstanceOf(ApiError);
    expect(structured.status).toBe(429);
    expect(retryAfterOf(structured)).toBe(7);

    stubFetch(async () => new Response("slow down", { status: 503, headers: { "Retry-After": "4" } }));
    const plain = await instance.optimize({ scenario }).catch((e) => e);
    expect(plain).toBeInstanceOf(ApiHttpError);
    expect(plain).toBeInstanceOf(ApiUnavailableError);
    expect(plain.message).toBe("Backend answered HTTP 503 for /optimize.");
    expect(retryAfterOf(plain)).toBe(4);
  });

  it("isTransient: only failures a later identical request can get past", () => {
    const e = (status: number) => new ApiError(status, "C", "m", [], "r");
    expect([429, 503].every((s) => isTransient(e(s)))).toBe(true);
    expect([400, 422, 502, 418].some((s) => isTransient(e(s)))).toBe(false);
    expect(isTransient(new ApiTimeoutError("t"))).toBe(true);
    expect(isTransient(new ApiNetworkError("n"))).toBe(true);
    expect(isTransient(new ApiHttpError("h", 504))).toBe(true);
    expect(isTransient(new ApiHttpError("h", 404))).toBe(false);
    expect(isTransient(new ApiUnavailableError("contract"))).toBe(false);
    expect(isTransient(new ModeMismatchError("mode"))).toBe(false);
  });
});

describe("automatic retries are limited to GET /health", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("retries a transient failure with doubling backoff and returns the first success", async () => {
    let call = 0;
    stubFetch(async () => {
      call += 1;
      if (call === 1) throw new TypeError("Failed to fetch");
      if (call === 2) return errorBody(503, "UNAVAILABLE");
      return Response.json(HEALTH_OK);
    });
    const { instance, waits } = retryingClient();
    await expect(instance.health()).resolves.toMatchObject({ status: "ok" });
    expect(fetchCalls()).toBe(3);
    expect(waits).toEqual([250, 500]);
  });

  it("gives up after the bounded number of retries and reports the last error", async () => {
    stubFetch(async () => errorBody(503, "UNAVAILABLE"));
    const { instance, waits } = retryingClient({ healthRetries: 2 });
    await expect(instance.health()).rejects.toBeInstanceOf(ApiError);
    expect(fetchCalls()).toBe(3); // 1 try + 2 retries, never more
    expect(waits).toHaveLength(2);
  });

  it("waits the server's Retry-After instead of the backoff", async () => {
    let call = 0;
    stubFetch(async () => {
      call += 1;
      return call === 1 ? errorBody(429, "THROTTLED", { "Retry-After": "2" }) : Response.json(HEALTH_OK);
    });
    const { instance, waits } = retryingClient();
    await instance.health();
    expect(waits).toEqual([2000]);
  });

  it("does not sit through a Retry-After longer than the cap: the UI gets the error and the number", async () => {
    stubFetch(async () => errorBody(429, "THROTTLED", { "Retry-After": "60" }));
    const { instance, waits } = retryingClient({ maxRetryWaitMs: 5000 });
    const error = await instance.health().catch((e) => e);
    expect(retryAfterOf(error)).toBe(60);
    expect(fetchCalls()).toBe(1);
    expect(waits).toEqual([]);
  });

  it.each([
    ["422", () => errorBody(422, "VALIDATION")],
    ["400", () => errorBody(400, "BAD_JSON")],
    ["a body that is not the contract", () => Response.json({ status: "ok" })],
  ])("does not retry %s", async (_name, make) => {
    stubFetch(async () => make());
    const { instance, waits } = retryingClient();
    await expect(instance.health()).rejects.toBeInstanceOf(Error);
    expect(fetchCalls()).toBe(1);
    expect(waits).toEqual([]);
  });

  it.each([
    ["optimize", (c: ApiClient) => c.optimize({ scenario })],
    ["confirm", (c: ApiClient) => c.confirm({ scenario })],
    ["extract", (c: ApiClient) => c.extract({ descriptions: [{ id: "devco", name: "DevCo", text: "x" }] })],
    ["simulate", (c: ApiClient) => c.simulate({ scenario, changes: [], text: "x", apply: false })],
    ["explain", (c: ApiClient) => c.explain({ scenario, result: {} as never })],
  ])("never retries a POST (%s) by itself, even for a transient failure", async (_name, call) => {
    stubFetch(async () => errorBody(503, "UNAVAILABLE"));
    const { instance, waits } = retryingClient();
    await expect(call(instance)).rejects.toBeInstanceOf(ApiError);
    expect(fetchCalls()).toBe(1);
    expect(waits).toEqual([]);
  });

  it("stops waiting for a retry when the caller aborts", async () => {
    stubFetch(async () => errorBody(503, "UNAVAILABLE"));
    const controller = new AbortController();
    const instance = new ApiClient({
      apiBaseUrl: "http://api.test",
      dataMode: "live",
      sleep: (_ms, signal) =>
        new Promise((_resolve, reject) => {
          signal?.addEventListener("abort", () => reject(new DOMException("a", "AbortError")));
          controller.abort();
        }),
    });
    await expect(instance.health({ signal: controller.signal })).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchCalls()).toBe(1);
  });
});
