import { defineConfig } from "@playwright/test";

// Run from the repo root with the Python venv active:  npm --prefix frontend run e2e
// Starts the local backend (fixture mode) and the Vite dev server unless already running.
export default defineConfig({
  testDir: "../tests/e2e",
  // tests/e2e is outside frontend/; its own tsconfig maps @playwright/test to frontend/node_modules.
  timeout: 30_000,
  fullyParallel: false,
  reporter: [["list"]],
  use: { baseURL: "http://127.0.0.1:5173" },
  webServer: [
    {
      command: "python -m backend.local",
      cwd: "..",
      url: "http://127.0.0.1:8000/health",
      reuseExistingServer: true,
      env: { APP_MODE: "fixture" },
    },
    {
      command: "npm run dev",
      url: "http://127.0.0.1:5173",
      reuseExistingServer: true,
      env: { VITE_API_BASE_URL: "http://127.0.0.1:8000", VITE_DATA_MODE: "fixture" },
    },
  ],
});
