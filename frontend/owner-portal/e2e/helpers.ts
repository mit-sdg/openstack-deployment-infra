import { expect, test, type Page } from '@playwright/test';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';

export { expect, test };

// Only the public GitHub reads are intercepted; all portal/provider traffic
// goes through the real HTTPS server. Private source reads use its deploy key.
test.beforeEach(async ({ context, page }) => {
  for (const origin of ['https://api.github.com/**', 'https://raw.githubusercontent.com/**'])
    await context.route(origin, (route) =>
      route.fulfill({ status: 404, json: {}, headers: { 'Access-Control-Allow-Origin': '*' } }),
    );
  await context.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (event) =>
      console.error('PORTAL_CSP:' + event.violatedDirective),
    );
  });
  const errors: string[] = [];
  const failures: { method: string; path: string; status: number; error: { code: string } }[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => {
    if (message.text().startsWith('PORTAL_CSP:')) errors.push(message.text());
  });
  page.on('request', (request) => {
    const host = new URL(request.url()).hostname;
    if (!['127.0.0.1', 'localhost', 'api.github.com', 'raw.githubusercontent.com'].includes(host))
      errors.push(`Unexpected host: ${host}`);
  });
  // Keep only credential-free metadata; the existing collector bounds and
  // sanitizes this again before CI uploads it. Successful runs need no images.
  page.on('response', (response) => {
    const url = new URL(response.url());
    if (url.pathname.startsWith('/api/') && response.status() >= 400 && failures.length < 24)
      failures.push({
        method: response.request().method(),
        path: url.pathname,
        status: response.status(),
        error: { code: 'HTTP_ERROR' },
      });
  });
  diagnostics.set(page, { errors, failures });
});
const diagnostics = new WeakMap<
  Page,
  {
    errors: string[];
    failures: { method: string; path: string; status: number; error: { code: string } }[];
  }
>();
test.afterEach(async ({ page }, info) => {
  const data = diagnostics.get(page)!;
  try {
    expect(data.errors).toEqual([]);
  } finally {
    if (info.status !== info.expectedStatus || data.errors.length) {
      const directory = path.resolve('../../.tmp/owner-portal-playwright/diagnostics');
      await mkdir(directory, { recursive: true });
      await writeFile(
        path.join(directory, 'desktop-light.json'),
        JSON.stringify({ version: 1, failures: data.failures }).slice(0, 8192),
      );
    }
  }
});

export async function signIn(page: Page, name: 'alice' | 'bob' | 'taylor') {
  await page.goto('/sign-in');
  await page.getByRole('link', { name: 'Sign in with your class account', exact: true }).click();
  await expect(page).toHaveURL(/^https:\/\/localhost:\d+\/connect\?/);
  await page.getByLabel('Username', { exact: true }).fill(name);
  await page.getByLabel('Password', { exact: true }).fill(`local-${name}-password`);
  await page.getByRole('button', { name: 'Allow', exact: true }).click();
  await expect(page).toHaveURL(/\/apps$/);
}

export async function createApp(page: Page, slug: string) {
  await page.goto('/apps/new');
  await page.getByLabel('App name').fill(slug);
  await page.getByRole('button', { name: 'Create app', exact: true }).click();
  await expect(page).toHaveURL(/\/apps\/[a-f0-9-]+\/configuration$/);
  return new URL(page.url()).pathname.split('/')[2];
}

export async function saveSettings(page: Page) {
  await page.getByLabel('Repository URL').fill('https://github.com/example/student-app');
  await page.getByLabel('Bun', { exact: false }).check();
  await page.getByLabel('Start script').fill('start');
  await page.getByLabel('Port', { exact: true }).fill('3000');
  await page.getByLabel('Health check path').fill('/health');
  await page.getByRole('button', { name: 'Save settings' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Settings saved.' })).toBeVisible();
}
