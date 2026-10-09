import { defineConfig } from '@playwright/test';

/**
 * The V1 acceptance walkthrough through the screens (P10, #1106). LOCAL ONLY: it walks a restored
 * mid-review copy of a real drawing set behind a local API with the reader off, so it cannot run in
 * CI (no client files there). How to run it: README, "Acceptance walkthrough".
 *
 * It uses the machine's installed Chrome (`channel: 'chrome'`), so no browser is downloaded.
 */
const out = process.env.GV_E2E_OUT ?? 'test-results';

export default defineConfig({
  testDir: './e2e',
  outputDir: `${out}/playwright`,
  timeout: 40 * 60_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list'], ['html', { outputFolder: `${out}/report`, open: 'never' }]],
  use: {
    baseURL: process.env.GV_E2E_BASE_URL ?? 'http://localhost:5173',
    channel: 'chrome',
    viewport: { width: 1440, height: 900 },
    // Video needs Playwright's ffmpeg (`npx playwright install ffmpeg`); GV_E2E_VIDEO=off runs without it.
    video: process.env.GV_E2E_VIDEO === 'off' ? 'off' : 'on',
    screenshot: 'on',
    trace: 'retain-on-failure',
    acceptDownloads: true,
    // A wrong turn fails in a minute, not at the end of the whole walk.
    actionTimeout: 60_000,
    navigationTimeout: 60_000,
  },
});
