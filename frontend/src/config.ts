// The ONLY two frontend environment variables (see .env.example). Public values only.
// Never put AWS credentials, tokens or secrets in VITE_* variables: they ship in the browser bundle.
import type { DataMode } from "../../contracts/models";

export interface AppConfig {
  apiBaseUrl: string;
  dataMode: DataMode;
  /** Set when VITE_DATA_MODE is not "fixture" or "live". The UI must show it, not guess. */
  configError: string | null;
}

export function readConfig(env: Record<string, string | undefined> = import.meta.env): AppConfig {
  const apiBaseUrl = (env.VITE_API_BASE_URL?.trim() || "http://127.0.0.1:8000").replace(/\/+$/, "");
  const rawMode = (env.VITE_DATA_MODE?.trim() || "fixture").toLowerCase();
  if (rawMode === "fixture" || rawMode === "live") {
    return { apiBaseUrl, dataMode: rawMode, configError: null };
  }
  return {
    apiBaseUrl,
    dataMode: "fixture",
    configError: `VITE_DATA_MODE must be "fixture" or "live", got "${rawMode}".`,
  };
}
