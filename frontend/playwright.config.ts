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
    command: "python e2e/control_server.py",
    cwd: frontendRoot,
    url: "http://127.0.0.1:19299/health",
    reuseExistingServer,
    timeout: 30_000,
    env: {
      PYTHONPATH: resolve(projectRoot, "src"),
      QB2API_E2E_CONTROL_PORT: "19299",
      QB2API_E2E_WORKER_PORT: "19301",
    },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
