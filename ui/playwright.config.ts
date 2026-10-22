import { defineConfig, devices } from "@playwright/test";

// Runs against a live stack (docker compose up): ServScope on :8000, the demo gateway on :8080.
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.SERVSCOPE_URL ?? "http://localhost:8000",
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
    launchOptions: process.env.PW_CHROMIUM_PATH ? { executablePath: process.env.PW_CHROMIUM_PATH } : {},
  },
});
