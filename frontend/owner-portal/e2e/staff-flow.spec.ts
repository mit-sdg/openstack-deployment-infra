import { expect, test, type Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';

const repository = path.resolve('../..');
const mode = process.env.OWNER_PORTAL_SMOKE_MODE ?? 'https';
if (!['https', 'http', 'vite'].includes(mode)) throw new Error('Invalid fixture mode');
const config = path.join(repository, `.tmp/e-smoke-${mode}/config.json`);
function fixture(action: 'grant' | 'revoke' | 'sentinel', appId = '') {
  // Test-owned loopback DB only: never a grant HTTP route or production recovery bypass.
  execFileSync(
    'uv',
    [
      'run',
      'python',
      '-c',
      `
import json, sys, time
from pathlib import Path
from openstack_platform.management.config import Config
from openstack_platform.management.broker.database import Database
from openstack_platform.management.broker.staff_admin import change_grant
config = Config.load(Path(sys.argv[1]))
assert config.development and config.state_directory.is_relative_to(Path.cwd() / '.tmp')
with Database(config).connect(write=True) as db:
    if sys.argv[2] == 'sentinel':
        row = db.execute('SELECT app_id,body FROM observations WHERE app_id=?', (sys.argv[3],)).fetchone()
        if row is not None:
            body = json.loads(row['body'])
            body['refs'] = {'environment': 'STAFF_SECRET_SENTINEL'}
            db.execute('UPDATE observations SET body=?,updated=? WHERE app_id=?', (json.dumps(body), time.time(), row['app_id']))
    else:
        row = db.execute("SELECT id,subject FROM users WHERE username='taylor' AND issuer=?", (config.issuer,)).fetchone()
        assert row is not None and row['subject'] == '44444444-4444-4444-8444-444444444444'
        change_grant(db, config, action=sys.argv[2], user_id=row['id'], issuer=config.issuer,
                     subject=row['subject'], review='playwright-fixture')
`,
      config,
      action,
      appId,
    ],
    { cwd: repository, stdio: 'pipe' },
  );
}
async function signIn(page: Page, username: string, staff = false) {
  await page.goto(staff ? '/signin?mode=staff' : '/sign-in');
  await page.getByLabel('Username', { exact: true }).fill(username);
  await page.getByLabel('Password', { exact: true }).fill(`local-${username}-password`);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page).toHaveURL(staff ? /\/staff\/owners$/ : /\/apps$/);
}
async function app(page: Page, slug: string) {
  const session = (await (await page.request.get('/api/v1/session')).json()).data;
  const headers = {
    Origin: new URL(page.url()).origin,
    'X-CSRF-Token': session.csrfToken,
    'Idempotency-Key': crypto.randomUUID(),
  };
  const known = (await (await page.request.get('/api/v1/apps')).json()).data.items;
  if (known.length) return { appId: known[0].applicationId as string, headers };
  const created = await page.request.post('/api/v1/apps', { headers, data: { slug } });
  expect([201, 202]).toContain(created.status());
  return { appId: (await created.json()).data.app.applicationId as string, headers };
}

for (const [layout, viewport, colorScheme] of [
  ['desktop-light', { width: 1440, height: 1000 }, 'light'],
  ['mobile-dark', { width: 390, height: 844 }, 'dark'],
] as const) {
  test(`staff and owner modes · ${layout}`, async ({ browser }) => {
    fixture('grant');
    const owner = await browser.newContext({ ignoreHTTPSErrors: true });
    const student = await browser.newContext({ ignoreHTTPSErrors: true });
    const staff = await browser.newContext({ viewport, colorScheme, ignoreHTTPSErrors: true });
    try {
      const ownerPage = await owner.newPage();
      const studentPage = await student.newPage();
      const staffPage = await staff.newPage();
      await signIn(ownerPage, 'taylor');
      const own = await app(ownerPage, 'instructor-project');
      await signIn(studentPage, 'alice');
      const other = await app(studentPage, 'student-project');
      expect((await ownerPage.request.get(`/api/v1/apps/${other.appId}`)).status()).toBe(404);
      expect((await studentPage.request.get(`/api/v1/apps/${own.appId}`)).status()).toBe(404);
      const csp: string[] = [];
      const network: string[] = [];
      await staff.addInitScript(() =>
        document.addEventListener('securitypolicyviolation', () =>
          console.error('STAFF_CSP_VIOLATION'),
        ),
      );
      staffPage.on('console', (event) => {
        if (event.text().includes('STAFF_CSP_VIOLATION')) csp.push(event.text());
      });
      staff.on('request', (request) => {
        if (!['127.0.0.1', 'localhost'].includes(new URL(request.url()).hostname))
          network.push(request.url());
      });
      await signIn(staffPage, 'taylor', true);
      expect((await ownerPage.request.get('/api/v1/session')).status()).toBe(200);
      expect((await staffPage.request.get('/api/v1/session')).status()).toBe(200);
      await expect(
        staffPage.getByRole('link', { name: 'Alice Student', exact: true }),
      ).toBeVisible();
      await expect(
        staffPage.getByRole('link', { name: 'Taylor Instructor', exact: true }),
      ).toBeVisible();
      await expect(staffPage.getByRole('navigation', { name: 'Staff pages' })).toBeVisible();
      await expect(
        staffPage.getByRole('link', { name: 'Create application', exact: true }),
      ).toHaveCount(0);
      const session = (await (await staffPage.request.get('/api/v1/session')).json()).data;
      const headers = {
        Origin: new URL(staffPage.url()).origin,
        'X-CSRF-Token': session.csrfToken,
        'Idempotency-Key': crypto.randomUUID(),
      };
      for (const appId of [own.appId, other.appId]) {
        for (const [method, endpoint, data] of [
          ['POST', '/api/v1/apps', { slug: 'forbidden-staff-project' }],
          [
            'PUT',
            `/api/v1/apps/${appId}/configuration`,
            {
              expectedRevision: 0,
              repository: 'https://github.com/example/student-app',
              branch: 'main',
              configuration: {
                schemaVersion: 1,
                build: {
                  runtime: 'node',
                  packages: ['.'],
                  buildScript: null,
                  startScript: 'start',
                },
                runtime: { port: 3000, healthPath: '/health' },
                storageBindings: [],
              },
            },
          ],
          [
            'POST',
            `/api/v1/apps/${appId}/deployments`,
            { configurationRevision: 1, commit: 'a'.repeat(40) },
          ],
          ['POST', `/api/v1/intents/${crypto.randomUUID()}/resume`, {}],
        ] as const) {
          const response = await staffPage.request.fetch(endpoint, { method, headers, data });
          expect(response.status()).toBe(403);
          expect((await response.json()).error.code).toBe('ACCESS_DENIED');
        }
      }
      expect(
        (await staffPage.request.get(`/api/v1/apps/${own.appId}/configuration`)).status(),
      ).toBe(403);
      await staffPage
        .getByRole('navigation', { name: 'Staff pages' })
        .getByRole('link', { name: 'Applications' })
        .click();
      await expect(
        staffPage.getByRole('link', { name: 'instructor-project', exact: true }),
      ).toBeVisible();
      await expect(
        staffPage.getByRole('link', { name: 'student-project', exact: true }),
      ).toBeVisible();
      await staffPage.getByRole('link', { name: 'student-project', exact: true }).click();
      await expect(
        staffPage.getByRole('heading', { name: 'student-project', exact: true }),
      ).toBeVisible();
      await expect(
        staffPage.getByRole('link', {
          name: 'https://github.com/example/student-app',
          exact: true,
        }),
      ).toBeVisible();
      await expect(staffPage.getByRole('link', { name: 'Deploy', exact: true })).toHaveCount(0);
      await staffPage.getByRole('link', { name: 'Deployments', exact: true }).click();
      await expect(staffPage.getByRole('heading', { name: 'Deployment history' })).toBeVisible();
      await expect(staffPage.getByRole('button', { name: /Resume|Deploy/ })).toHaveCount(0);
      await staffPage
        .getByRole('navigation', { name: 'Staff pages' })
        .getByRole('link', { name: 'Operations' })
        .click();
      await expect(
        staffPage.getByRole('heading', { name: 'Operations', exact: true }),
      ).toBeVisible();
      await expect(staffPage.getByRole('button', { name: 'Resume', exact: true })).toHaveCount(0);
      fixture('sentinel', own.appId);
      const response = await staffPage.request.get(`/api/v1/staff/apps/${own.appId}`, { headers });
      expect(response.status()).toBe(200);
      expect(await response.text()).not.toContain('STAFF_SECRET_SENTINEL');
      await staffPage.goto(`/staff/apps/${own.appId}`);
      await expect(
        staffPage.getByRole('heading', { name: 'instructor-project', exact: true }),
      ).toBeVisible();
      expect(await staffPage.content()).not.toContain('STAFF_SECRET_SENTINEL');
      await mkdir(path.join(repository, '.tmp/owner-portal-playwright/screenshots'), {
        recursive: true,
      });
      await staffPage.screenshot({
        path: path.join(
          repository,
          `.tmp/owner-portal-playwright/screenshots/staff-${mode}-${layout}.png`,
        ),
        fullPage: true,
      });
      fixture('revoke');
      expect((await staffPage.request.get('/api/v1/session')).status()).toBe(401);
      expect((await ownerPage.request.get('/api/v1/session')).status()).toBe(401);
      await staffPage.getByRole('button', { name: 'Refresh', exact: true }).click();
      await expect(staffPage).toHaveURL(/\/signin\?mode=staff$/);
      await expect(staffPage.getByRole('navigation', { name: 'Staff pages' })).toHaveCount(0);
      expect(csp).toHaveLength(0);
      expect(network).toHaveLength(0);
    } finally {
      fixture('grant');
      await owner.close();
      await student.close();
      await staff.close();
    }
  });
}
test('ordinary credentials cannot mint staff authority; staff expiry returns to credential entry', async ({
  page,
}) => {
  await page.goto('/signin?mode=staff');
  await page.getByLabel('Username', { exact: true }).fill('bob');
  await page.getByLabel('Password', { exact: true }).fill('wrong-fixture');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Username or password is incorrect');
  await page.getByLabel('Password', { exact: true }).fill('local-bob-password');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('not available for this account');
  await page.clock.install();
  await signIn(page, 'taylor', true);
  await page.clock.fastForward(3600001);
  await expect(page).toHaveURL(/\/signin\?mode=staff$/);
  await expect(
    page.getByRole('heading', { name: 'Staff sign-in with your class account' }),
  ).toBeVisible();
});
