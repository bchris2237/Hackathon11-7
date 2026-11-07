// Typed v1 API client. One code path for both modes: it always talks to the backend at
// VITE_API_BASE_URL. It never substitutes local data when a call fails, and it refuses a
// response whose `mode` differs from the configured VITE_DATA_MODE (no silent mixing).
import type {
  ConfirmRequest,
  ConfirmResponse,
  DataMode,
  ErrorEnvelope,
  ExplainRequest,
  ExplainResponse,
  ExtractRequest,
  ExtractResponse,
  HealthResponse,
  OptimizeRequest,
  OptimizeResponse,
  Result,
  SimulateRequest,
  SimulateResponse,
} from "../../../contracts/models";

export const DEFAULT_TIMEOUT_MS = 15_000;

/** Backend unreachable, timed out, or answered something that is not the v1 contract. */
export class ApiUnavailableError extends Error {}

/** No answer within the client timeout. The server may or may not have processed the request. */
export class ApiTimeoutError extends ApiUnavailableError {}

/** The request never completed: connection refused, dropped, blocked (for example by CORS). */
export class ApiNetworkError extends ApiUnavailableError {}

/** An HTTP error status whose body is not the v1 error envelope. */
export class ApiHttpError extends ApiUnavailableError {
  constructor(
    message: string,
    readonly status: number,
    readonly retryAfterSeconds?: number,
  ) {
    super(message);
  }
}

/** The backend answered with the v1 error envelope. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly fields: string[],
    readonly requestId: string,
    /** From the Retry-After response header, when the server sent a usable one. */
    readonly retryAfterSeconds?: number,
  ) {
    super(message);
  }
}

/** The response is valid but labelled with a different mode than the one configured. */
export class ModeMismatchError extends Error {}

export interface ClientOptions {
  apiBaseUrl: string;
  dataMode: DataMode;
  timeoutMs?: number;
  /** Automatic retries for GET /health only (a safe read). Mutations are never retried automatically. */
  healthRetries?: number;
  /** First backoff wait for an automatic retry; doubles each time. */
  retryBaseMs?: number;
  /** Longest wait the client will sit through on its own. A longer Retry-After is handed to the UI. */
  maxRetryWaitMs?: number;
  /** Replaceable for tests. Must reject with an AbortError when the signal aborts. */
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
}

export const DEFAULT_HEALTH_RETRIES = 2;
export const DEFAULT_RETRY_BASE_MS = 250;
export const DEFAULT_MAX_RETRY_WAIT_MS = 5_000;

function defaultSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException("Aborted", "AbortError"));
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * Retry-After as whole seconds. Accepts delta-seconds or an HTTP date; anything else is
 * ignored (undefined). Never negative.
 */
export function parseRetryAfter(header: string | null | undefined, now: number = Date.now()): number | undefined {
  if (header === null || header === undefined) return undefined;
  const raw = header.trim();
  if (/^\d{1,9}$/.test(raw)) return Number(raw);
  // HTTP-date ("Wed, 21 Oct 2026 07:28:00 GMT") only. Date.parse alone would read "1.5" or "-5" as dates.
  if (!/^[A-Za-z]{3,9},\s/.test(raw)) return undefined;
  const when = Date.parse(raw);
  if (Number.isNaN(when)) return undefined;
  return Math.max(0, Math.ceil((when - now) / 1000));
}

/** True for failures that a later identical request may well get past. */
export function isTransient(error: unknown): boolean {
  if (error instanceof ApiTimeoutError || error instanceof ApiNetworkError) return true;
  if (error instanceof ApiError) return error.status === 429 || error.status === 503;
  if (error instanceof ApiHttpError) return [429, 502, 503, 504].includes(error.status);
  return false;
}

/** Seconds the server asked us to wait, if any. */
export function retryAfterOf(error: unknown): number | undefined {
  return error instanceof ApiError || error instanceof ApiHttpError ? error.retryAfterSeconds : undefined;
}

export interface CallOptions {
  signal?: AbortSignal;
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  if (typeof value !== "object" || value === null) return false;
  const e = (value as { error?: unknown }).error;
  if (typeof e !== "object" || e === null) return false;
  const r = e as Record<string, unknown>;
  return (
    typeof r.code === "string" &&
    typeof r.message === "string" &&
    Array.isArray(r.fields) &&
    typeof r.requestId === "string"
  );
}

export function isHealthResponse(value: unknown): value is HealthResponse {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    v.status === "ok" &&
    v.schemaVersion === "1" &&
    typeof v.commit === "string" &&
    (v.mode === "live" || v.mode === "fixture")
  );
}

/** Structural check of a v1 Result, including "numbers are null only when infeasible". */
export function isResult(value: unknown): value is Result {
  if (typeof value !== "object" || value === null) return false;
  const r = value as Record<string, any>;
  if (r.schemaVersion !== "1" || !Array.isArray(r.assignments) || !Array.isArray(r.reasons)) return false;
  if (!r.reasons.every((x: any) => typeof x?.code === "string" && typeof x?.message === "string")) return false;
  if (r.status === "infeasible") {
    return r.assignments.length === 0 && r.businessMetrics === null && r.totals === null && r.certificate === null;
  }
  if (r.status !== "collaboration" && r.status !== "no_exchange") return false;
  const metricKeys = ["baselineMinutes", "productionMinutes", "coordinationMinutes", "effortMinutes", "netSavingsMinutes"];
  return (
    r.assignments.every(
      (a: any) =>
        typeof a?.deliverableId === "string" && typeof a?.providerId === "string" && Number.isInteger(a?.minutes),
    ) &&
    Array.isArray(r.businessMetrics) &&
    r.businessMetrics.every(
      (m: any) => typeof m?.businessId === "string" && metricKeys.every((k) => Number.isInteger(m?.[k])),
    ) &&
    typeof r.totals === "object" &&
    r.totals !== null &&
    [...metricKeys, "grossSavingsMinutes"].every((k) => Number.isInteger(r.totals[k]))
  );
}

export function isExtractResponse(value: unknown): value is ExtractResponse {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, any>;
  return (
    typeof v.draft === "object" &&
    v.draft !== null &&
    Array.isArray(v.draft.businesses) &&
    Array.isArray(v.draft.deliverables) &&
    Array.isArray(v.missingFields) &&
    v.missingFields.every((f: unknown) => typeof f === "string") &&
    Array.isArray(v.provenance) &&
    v.provenance.every((p: any) => typeof p?.path === "string" && typeof p?.quote === "string") &&
    !("confirmed" in v.draft) // extraction never confirms
  );
}

/** Readable one-line text for any error thrown by this module. */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    const fields = error.fields.length ? ` (fields: ${error.fields.join(", ")})` : "";
    return `${error.message}${fields} [${error.code}, request ${error.requestId}]`;
  }
  if (error instanceof ApiUnavailableError || error instanceof ModeMismatchError) return error.message;
  return "Unexpected error talking to the backend.";
}

export class ApiClient {
  private readonly apiBaseUrl: string;
  private readonly timeoutMs: number;
  private readonly healthRetries: number;
  private readonly retryBaseMs: number;
  private readonly maxRetryWaitMs: number;
  private readonly sleep: (ms: number, signal?: AbortSignal) => Promise<void>;
  readonly dataMode: DataMode;

  constructor(options: ClientOptions) {
    this.apiBaseUrl = options.apiBaseUrl.replace(/\/+$/, "");
    this.dataMode = options.dataMode;
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.healthRetries = options.healthRetries ?? DEFAULT_HEALTH_RETRIES;
    this.retryBaseMs = options.retryBaseMs ?? DEFAULT_RETRY_BASE_MS;
    this.maxRetryWaitMs = options.maxRetryWaitMs ?? DEFAULT_MAX_RETRY_WAIT_MS;
    this.sleep = options.sleep ?? defaultSleep;
  }

  private async request(path: string, init: RequestInit, signal?: AbortSignal): Promise<unknown> {
    const timeout = AbortSignal.timeout(this.timeoutMs);
    const combined = signal ? AbortSignal.any([signal, timeout]) : timeout;
    let response: Response;
    try {
      response = await fetch(`${this.apiBaseUrl}${path}`, { ...init, signal: combined });
    } catch (error) {
      // A caller abort (superseded request, unmount) is not an error to display.
      if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
      if (timeout.aborted || isAbort(error)) {
        throw new ApiTimeoutError(`The backend at ${this.apiBaseUrl} did not answer within ${this.timeoutMs / 1000} s.`);
      }
      throw new ApiNetworkError(`Cannot reach the backend at ${this.apiBaseUrl}. Is it running?`);
    }
    let body: unknown = undefined;
    try {
      body = await response.json();
    } catch {
      if (response.ok) throw new ApiUnavailableError(`Backend ${path} did not return JSON.`);
    }
    if (!response.ok) {
      const retryAfter = parseRetryAfter(response.headers.get("Retry-After"));
      if (isErrorEnvelope(body)) {
        const { code, message, fields, requestId } = body.error;
        throw new ApiError(response.status, code, message, fields, requestId, retryAfter);
      }
      throw new ApiHttpError(`Backend answered HTTP ${response.status} for ${path}.`, response.status, retryAfter);
    }
    return body;
  }

  private post<TReq, TRes extends { mode: DataMode }>(path: string, payload: TReq, options?: CallOptions) {
    return this.request(
      path,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) },
      options?.signal,
    ).then((body) => this.checkMode(path, body as TRes));
  }

  private checkMode<T extends { mode: DataMode }>(path: string, body: T): T {
    if (typeof body !== "object" || body === null || (body.mode !== "live" && body.mode !== "fixture")) {
      throw new ApiUnavailableError(`Backend ${path} response does not match the v1 contract.`);
    }
    if (body.mode !== this.dataMode) {
      throw new ModeMismatchError(
        `The app is in ${this.dataMode} mode but the backend answered ${path} in ${body.mode} mode. Result discarded.`,
      );
    }
    return body;
  }

  /**
   * GET /health. The only call that retries by itself, because it is a safe read: up to
   * `healthRetries` more attempts for transient failures, waiting the server's Retry-After
   * when given (and short enough), else a doubling backoff. POST calls are never retried here.
   */
  async health(options?: CallOptions): Promise<HealthResponse> {
    for (let attempt = 0; ; attempt++) {
      try {
        return await this.healthOnce(options?.signal);
      } catch (error) {
        if (attempt >= this.healthRetries || !isTransient(error)) throw error;
        const asked = retryAfterOf(error);
        const waitMs = asked !== undefined ? asked * 1000 : this.retryBaseMs * 2 ** attempt;
        // A longer wait than we are willing to sit through goes to the user instead.
        if (waitMs > this.maxRetryWaitMs) throw error;
        await this.sleep(waitMs, options?.signal);
      }
    }
  }

  private async healthOnce(signal?: AbortSignal): Promise<HealthResponse> {
    const body = await this.request("/health", { method: "GET" }, signal);
    if (!isHealthResponse(body)) {
      throw new ApiUnavailableError("Backend /health response does not match the v1 contract.");
    }
    return body;
  }

  async extract(req: ExtractRequest, options?: CallOptions): Promise<ExtractResponse> {
    const body = await this.post<ExtractRequest, ExtractResponse>("/extract", req, options);
    if (!isExtractResponse(body)) {
      throw new ApiUnavailableError("Backend /extract response does not match the v1 contract.");
    }
    return body;
  }
  async confirm(req: ConfirmRequest, options?: CallOptions): Promise<ConfirmResponse> {
    const body = await this.request(
      "/confirm",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(req) },
      options?.signal,
    );
    const scenario = (body as { scenario?: { confirmed?: unknown; schemaVersion?: unknown } } | null)?.scenario;
    if (!scenario || scenario.confirmed !== true || scenario.schemaVersion !== "1") {
      throw new ApiUnavailableError("Backend /confirm response does not match the v1 contract.");
    }
    return body as ConfirmResponse;
  }
  async optimize(req: OptimizeRequest, options?: CallOptions): Promise<OptimizeResponse> {
    const body = await this.post<OptimizeRequest, OptimizeResponse>("/optimize", req, options);
    if (!isResult(body.result)) {
      throw new ApiUnavailableError("Backend /optimize response does not match the v1 contract.");
    }
    return body;
  }
  async simulate(req: SimulateRequest, options?: CallOptions): Promise<SimulateResponse> {
    const body = await this.post<SimulateRequest, SimulateResponse>("/simulate", req, options);
    const scenario = body.scenario as { confirmed?: unknown; schemaVersion?: unknown } | undefined;
    if (
      !Array.isArray(body.proposedChanges) ||
      typeof body.requiresConfirmation !== "boolean" ||
      !scenario ||
      scenario.confirmed !== true ||
      scenario.schemaVersion !== "1" ||
      (body.result !== null && !isResult(body.result))
    ) {
      throw new ApiUnavailableError("Backend /simulate response does not match the v1 contract.");
    }
    return body;
  }
  async explain(req: ExplainRequest, options?: CallOptions): Promise<ExplainResponse> {
    const body = await this.post<ExplainRequest, ExplainResponse>("/explain", req, options);
    if (typeof body.text !== "string" || body.text.length === 0 || body.text.length > 5000) {
      throw new ApiUnavailableError("Backend /explain response does not match the v1 contract.");
    }
    return body;
  }
}

/** Back-compat helper used by the Phase 0 shell. */
export async function fetchHealth(apiBaseUrl: string, signal?: AbortSignal): Promise<HealthResponse> {
  return new ApiClient({ apiBaseUrl, dataMode: "live" }).health({ signal });
}
