import { defineConfig } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const mode = process.env.OWNER_PORTAL_SMOKE_MODE ?? 'https';
if (!['https', 'http', 'vite'].includes(mode)) throw new Error('Invalid smoke mode');
const port = mode === 'https' ? 9543 : mode === 'http' ? 9553 : 9563;
const origin = `${mode === 'https' ? 'https' : 'http'}://127.0.0.1:${port}`;
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 45000,
  retries: 0,
  reporter: 'list',
  outputDir: path.join(repository, '.tmp/owner-portal-playwright/artifacts'),
  use: {
    baseURL: origin,
    browserName: 'chromium',
    ignoreHTTPSErrors: true,
    trace: 'off',
    video: 'off',
    screenshot: 'off',
  },
  webServer: {
    gracefulShutdown: { signal: 'SIGTERM', timeout: 10000 },
    command: `uv run python -m openstack_platform.management.dev --state .tmp/e-smoke-${mode} --port ${port} --provider-port ${port + 1}${mode === 'https' ? '' : ' --http'}${mode === 'vite' ? ' --vite' : ''}`,
    cwd: repository,
    url: `${origin}/sign-in`,
    ignoreHTTPSErrors: true,
    reuseExistingServer: false,
    timeout: 30000,
  },
});
