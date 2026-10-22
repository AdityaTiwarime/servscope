import { defineConfig, devices } from "@playwright/test";

// Runs against a live stack (docker compose up): ServScope on :8000, the demo gateway on :8080.
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  retries: 0,
  // One at a time: every test shares one live stack, and the alert test breaks payments on
  // purpose, which would leak into whatever ran beside it.
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.SERVSCOPE_URL ?? "http://localhost:8000",
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
    launchOptions: process.env.PW_CHROMIUM_PATH ? { executablePath: process.env.PW_CHROMIUM_PATH } : {},
  },
});
