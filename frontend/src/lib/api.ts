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

/** The backend answered with the v1 error envelope. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly fields: string[],
    readonly requestId: string,
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
  readonly dataMode: DataMode;

  constructor(options: ClientOptions) {
    this.apiBaseUrl = options.apiBaseUrl.replace(/\/+$/, "");
    this.dataMode = options.dataMode;
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
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
        throw new ApiUnavailableError(`The backend at ${this.apiBaseUrl} did not answer within ${this.timeoutMs / 1000} s.`);
      }
      throw new ApiUnavailableError(`Cannot reach the backend at ${this.apiBaseUrl}. Is it running?`);
    }
    let body: unknown = undefined;
    try {
      body = await response.json();
    } catch {
      if (response.ok) throw new ApiUnavailableError(`Backend ${path} did not return JSON.`);
    }
    if (!response.ok) {
      if (isErrorEnvelope(body)) {
        const { code, message, fields, requestId } = body.error;
        throw new ApiError(response.status, code, message, fields, requestId);
      }
      throw new ApiUnavailableError(`Backend answered HTTP ${response.status} for ${path}.`);
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

  /** GET /health. */
  async health(options?: CallOptions): Promise<HealthResponse> {
    const body = await this.request("/health", { method: "GET" }, options?.signal);
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
  simulate(req: SimulateRequest, options?: CallOptions): Promise<SimulateResponse> {
    return this.post("/simulate", req, options);
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
