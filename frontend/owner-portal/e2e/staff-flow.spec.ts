import { expect, test, type Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import path from 'node:path';

const repository = path.resolve('../..');
const mode = process.env.OWNER_PORTAL_SMOKE_MODE ?? 'https';
if (!['https', 'http', 'vite'].includes(mode)) throw new Error('Invalid fixture mode');
const port = process.env.OWNER_PORTAL_SMOKE_PORT;
const config = path.join(
  repository,
  `.tmp/e-accounts-${mode}${port ? `-${port}` : ''}/config.json`,
);
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
  await page.getByRole('link', { name: 'Create app', exact: true }).click();
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
      const managedReads: string[] = [];
      page.on('response', (response) => {
        if (new URL(response.url()).pathname.startsWith('/api/v1/admin-apps'))
          managedReads.push(`${response.status()} ${new URL(response.url()).pathname}`);
      });
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
      await staffPage.getByRole('link', { name: 'Staff', exact: true }).click();
      await expect(staffPage.getByRole('heading', { name: 'Owners', exact: true })).toBeVisible();
      await expect(
        staffPage.getByRole('link', { name: 'Alice Student', exact: true }),
      ).toBeVisible();
      await staffPage.goto('/admin/accounts');
      await expect(
        staffPage.getByRole('heading', { name: "You don't have access to this page" }),
      ).toBeVisible();
      await ownerPage.goto('/admin/audit');
      await expect(
        ownerPage.getByRole('heading', { name: "You don't have access to this page" }),
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
      await page.getByRole('link', { name: 'Audit log', exact: true }).click();
      await expect(page.getByRole('heading', { name: 'Admin audit', exact: true })).toBeVisible();
      await expect(page.getByText('account_invited', { exact: true }).first()).toBeVisible();
      await page.getByRole('link', { name: 'All apps', exact: true }).click();
      await expect(
        page.getByRole('heading', { name: 'Manage applications', exact: true }),
      ).toBeVisible();
      const operatorId =
        layout === 'desktop-light'
          ? '00000000-0000-4000-8000-000000000081'
          : '00000000-0000-4000-8000-000000000083';
      const liveSession = (await (await page.request.get('/api/v1/session')).json()).data;
      const known = await page.request.get(`/api/v1/admin-apps/${operatorId}`, {
        headers: { 'X-CSRF-Token': liveSession.csrfToken },
      });
      if (known.status() === 200) {
        await page.goto(`/admin/apps/${operatorId}`);
      } else {
        expect(known.status()).toBe(404);
        await page.getByLabel('Controller application UUID').fill(operatorId);
        await page
          .getByLabel('Portal sign-in depends on this app — confirm adoption if this is Commons')
          .check();
        await page.getByRole('button', { name: 'Adopt application', exact: true }).click();
      }
      await expect(page).toHaveURL(new RegExp(`/admin/apps/${operatorId}$`));
      try {
        await expect(
          page.getByText('Portal sign-in depends on this app', { exact: true }),
        ).toBeVisible({ timeout: 15000 });
      } catch (error) {
        console.log(managedReads.join('\n'));
        console.log(await page.locator('main').innerText());
        throw error;
      }
      await expect(page.getByLabel('Repository URL')).toHaveValue(
        'https://github.com/example/class-app',
      );
      const imported = await page.request.get(`/api/v1/admin-apps/${operatorId}/configuration`, {
        headers: { 'X-CSRF-Token': liveSession.csrfToken },
      });
      const initialRevision = (await imported.json()).data.revision;
      await page.getByLabel('Health path').fill('/ready');
      await page.getByRole('button', { name: 'Save configuration', exact: true }).click();
      await expect(page.getByLabel('Health path')).toHaveValue('/ready');
      await expect(
        page.getByText(`Revision ${initialRevision + 1}`, { exact: true }),
      ).toBeVisible();
      await page.getByRole('button', { name: 'Deploy application', exact: true }).click();
      const dialog = page.getByRole('dialog', { name: 'Deploy application', exact: true });
      await expect(dialog).toContainText('A retained primary IPv4 requires maintenance.');
      await expect(dialog).toContainText('Brief cutover downtime');
      await expect(
        dialog.getByRole('button', { name: 'Confirm deployment', exact: true }),
      ).toBeDisabled();
      await expect(dialog).toContainText('current accepted sizing is preserved');
      await expect(dialog).toContainText('Portal sign-in depends on this app');
      await staffPage.goto('/admin/apps');
      await expect(
        staffPage.getByRole('heading', { name: "You don't have access to this page" }),
      ).toBeVisible();
      expect((await staffPage.request.get('/api/v1/admin-apps')).status()).toBe(403);
      expect((await ownerPage.request.get('/api/v1/admin-apps')).status()).toBe(403);
    } finally {
      await admin.close();
      await staff.close();
      await owner.close();
      await other.close();
    }
  });
}
