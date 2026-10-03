import { expect, test, type Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import path from 'node:path';

const repository = path.resolve('../..');
const mode = process.env.OWNER_PORTAL_SMOKE_MODE ?? 'https';
if (!['https', 'http', 'vite'].includes(mode)) throw new Error('Invalid fixture mode');
const config = path.join(repository, `.tmp/e-accounts-${mode}/config.json`);
const password = 'private fixture phrase 927184';
function bootstrapUrl() {
  return execFileSync(
    'uv',
    [
      'run',
      'python',
      '-c',
      `
from pathlib import Path
import sys
from openstack_platform.management.config import Config
from openstack_platform.management.broker.bootstrap import issue
config = Config.load(Path(sys.argv[1]))
assert config.development and config.state_directory.is_relative_to(Path.cwd() / '.tmp')
print(issue(config))
`,
      config,
    ],
    { cwd: repository, stdio: 'pipe' },
  )
    .toString()
    .trim();
}
function code(secret: string) {
  return execFileSync(
    'uv',
    [
      'run',
      'python',
      '-c',
      `
import sys,time
from openstack_platform.management.broker.local_security import totp_code
print(totp_code(sys.argv[1], int(time.time()//30)))
`,
      secret,
    ],
    { cwd: repository, stdio: 'pipe' },
  )
    .toString()
    .trim();
}
async function commons(page: Page, name: string) {
  await page.goto('/sign-in');
  await page.getByLabel('Username', { exact: true }).fill(name);
  await page.getByLabel('Password', { exact: true }).fill(`local-${name}-password`);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page).toHaveURL(/\/apps$/);
}
async function createApp(page: Page, slug: string) {
  const known = (await (await page.request.get('/api/v1/apps')).json()).data.items;
  if (known.length) return known[0].applicationId as string;
  await page.getByRole('link', { name: 'Create application', exact: true }).click();
  await page.getByLabel('Application name').fill(slug);
  await page.getByRole('button', { name: 'Create application', exact: true }).click();
  await expect(page).toHaveURL(/\/configuration$/);
  return new URL(page.url()).pathname.split('/')[2];
}
for (const [layout, viewport, colorScheme] of [
  ['desktop-light', { width: 1440, height: 1000 }, 'light'],
  ['mobile-dark', { width: 390, height: 844 }, 'dark'],
] as const) {
  test(`local admin bootstrap, invite and role boundaries · ${layout}`, async ({ browser }) => {
    const admin = await browser.newContext({ viewport, colorScheme, ignoreHTTPSErrors: true });
    const staff = await browser.newContext({ ignoreHTTPSErrors: true });
    const owner = await browser.newContext({ ignoreHTTPSErrors: true });
    const other = await browser.newContext({ ignoreHTTPSErrors: true });
    try {
      const page = await admin.newPage();
      const staffPage = await staff.newPage();
      const ownerPage = await owner.newPage();
      const otherPage = await other.newPage();
      const suffix = crypto.randomUUID().replaceAll('-', '').slice(0, 8);
      const url = bootstrapUrl();
      const token = new URL(url).hash.slice(1);
      const requestUrls: string[] = [];
      admin.on('request', (request) => requestUrls.push(request.url()));
      await page.goto(url);
      await expect(page).toHaveURL(/\/setup$/);
      await page.getByLabel('Local username', { exact: true }).fill('admin' + suffix);
      await page.getByLabel('New password', { exact: true }).fill(password);
      await page.getByRole('button', { name: 'Continue', exact: true }).click();
      const secret = await page.getByTestId('totp-secret').textContent();
      expect(secret).toBeTruthy();
      await page.getByLabel('Authentication code', { exact: true }).fill(code(secret!));
      await page.getByRole('button', { name: 'Finish enrollment', exact: true }).click();
      await expect(page).toHaveURL(/\/admin\/accounts$/);
      await expect(page.getByRole('heading', { name: 'Accounts', exact: true })).toBeVisible();
      expect(requestUrls.every((value) => !value.includes(token))).toBe(true);
      await page.getByLabel('Local username', { exact: true }).fill('staff' + suffix);
      await page.getByLabel('Display name', { exact: true }).fill('Local Staff');
      await page.getByLabel('Account role', { exact: true }).selectOption('staff');
      await page.getByRole('button', { name: 'Create invitation', exact: true }).click();
      const invitation = await page.getByLabel('Account setup link').first().inputValue();
      await staffPage.goto(invitation);
      await staffPage.getByLabel('New password', { exact: true }).fill(password);
      await staffPage.getByRole('button', { name: 'Continue', exact: true }).click();
      await staffPage.getByRole('button', { name: 'Finish enrollment', exact: true }).click();
      await expect(staffPage).toHaveURL(/\/apps$/);
      const own = await createApp(staffPage, 'local-staff-' + suffix);
      await commons(ownerPage, 'alice');
      const foreign = await createApp(ownerPage, 'student-project');
      expect((await staffPage.request.get(`/api/v1/apps/${foreign}`)).status()).toBe(404);
      expect((await ownerPage.request.get(`/api/v1/apps/${own}`)).status()).toBe(404);
      await staffPage.getByRole('link', { name: 'Staff catalog', exact: true }).click();
      await expect(staffPage.getByRole('heading', { name: 'Owners', exact: true })).toBeVisible();
      await expect(
        staffPage.getByRole('link', { name: 'Alice Student', exact: true }),
      ).toBeVisible();
      await staffPage.goto('/admin/accounts');
      await expect(
        staffPage.getByRole('heading', { name: 'Admin access unavailable' }),
      ).toBeVisible();
      await ownerPage.goto('/admin/audit');
      await expect(
        ownerPage.getByRole('heading', { name: 'Admin access unavailable' }),
      ).toBeVisible();
      expect((await staffPage.request.get('/api/v1/accounts')).status()).toBe(403);
      expect((await ownerPage.request.get('/api/v1/account-audit')).status()).toBe(403);
      await otherPage.goto(invitation);
      await expect(otherPage.getByRole('alert')).toContainText('unavailable or expired');
      const replay = await browser.newContext({ ignoreHTTPSErrors: true });
      try {
        const retry = await replay.newPage();
        await retry.goto(url);
        await expect(retry.getByRole('alert')).toContainText('unavailable or expired');
      } finally {
        await replay.close();
      }
      await page.getByRole('link', { name: 'Audit', exact: true }).click();
      await expect(page.getByRole('heading', { name: 'Admin audit', exact: true })).toBeVisible();
      await expect(page.getByText('account_invited', { exact: true }).first()).toBeVisible();
    } finally {
      await admin.close();
      await staff.close();
      await owner.close();
      await other.close();
    }
  });
}
