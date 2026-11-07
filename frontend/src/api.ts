import type { HealthResponse } from "../../contracts/models";

export class ApiUnavailableError extends Error {}

function isHealthResponse(value: unknown): value is HealthResponse {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    v.status === "ok" &&
    v.schemaVersion === "1" &&
    typeof v.commit === "string" &&
    (v.mode === "live" || v.mode === "fixture")
  );
}

/** GET /health. Throws ApiUnavailableError with a plain message on any failure. */
export async function fetchHealth(apiBaseUrl: string, signal?: AbortSignal): Promise<HealthResponse> {
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl}/health`, { signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiUnavailableError(`Cannot reach the backend at ${apiBaseUrl}. Is it running?`);
  }
  if (!response.ok) {
    throw new ApiUnavailableError(`Backend answered HTTP ${response.status} for /health.`);
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new ApiUnavailableError("Backend /health did not return JSON.");
  }
  if (!isHealthResponse(body)) {
    throw new ApiUnavailableError("Backend /health response does not match the v1 contract.");
  }
  return body;
}
