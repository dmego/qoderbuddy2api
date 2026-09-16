// Playwright config for the admin console acceptance tests.
//
// The service under test is the Go binary, started on a private port with its
// own throwaway data directory and freshly generated keys, so a run can never
// touch a developer's real .env or database.
import { defineConfig, devices } from "@playwright/test";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const frontendRoot = dirname(fileURLToPath(import.meta.url));
const projectRoot = resolve(frontendRoot, "..");
const reuseExistingServer = process.env.QB2API_E2E_REUSE_SERVER === "1";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 8_000 },
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:19299",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "bash e2e/run-server.sh",
    cwd: frontendRoot,
    url: "http://127.0.0.1:19299/health",
    reuseExistingServer,
    timeout: 60_000,
    env: {
      QB2API_E2E_ROOT: projectRoot,
    },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
