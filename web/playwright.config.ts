import { loadEnvConfig } from "@next/env";
import { defineConfig, devices } from "@playwright/test";

import { getWebBasePath } from "./lib/web-route";

loadEnvConfig(process.cwd());

const port = Number(process.env.WEB_E2E_PORT ?? 3212);
const baseURL = `http://localhost:${port}`;
const webBasePath = getWebBasePath();

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  use: {
    baseURL,
    colorScheme: "light",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "desktop-chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 1000 },
      },
    },
    {
      name: "mobile-chromium",
      use: {
        ...devices["iPhone 13"],
        browserName: "chromium",
      },
    },
  ],
  webServer: {
    command: `npm run dev -- --hostname localhost --port ${port}`,
    url: `${baseURL}${webBasePath}`,
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
