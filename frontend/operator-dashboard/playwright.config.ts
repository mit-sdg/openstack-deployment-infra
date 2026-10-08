import { defineConfig } from "@playwright/test";
import { fileURLToPath } from "node:url";
import path from "node:path";

const repository = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
const scenarios = ["mixed", "healthy", "outage", "empty", "unreachable"];
export default defineConfig({
  testDir: "./e2e",
  // Each test owns its browser context and screenshot paths; preview data
  // is synthetic and read-only, so two workers can share the servers.
  fullyParallel: true,
  workers: 2,
  timeout: 30000,
  retries: 0,
  reporter: "list",
  outputDir: path.join(repository, ".tmp/dashboard-playwright/artifacts"),
  use: {
    browserName: "chromium",
    trace: "off",
    video: "off",
    screenshot: "off",
  },
  webServer: scenarios.map((scenario, index) => ({
    command: `uv run python -m openstack_platform.dashboard.preview --port ${8480 + index} --scenario ${scenario}`,
    cwd: repository,
    url: `http://127.0.0.1:${8480 + index}`,
    reuseExistingServer: false,
    gracefulShutdown: { signal: "SIGTERM" as const, timeout: 10000 },
    timeout: 30000,
  })),
});
