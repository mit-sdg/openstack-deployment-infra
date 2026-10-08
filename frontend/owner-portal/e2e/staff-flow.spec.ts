import { expect, test, type Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { mkdir, writeFile } from 'node:fs/promises';

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
  await page.bringToFront();
  await page.goto('/sign-in');
  // The fake class site asks who is signing in, then sends the browser back.
  await page.getByRole('link', { name: 'Sign in with your class account', exact: true }).click();
  await page.getByLabel('Username', { exact: true }).fill(name);
  await page.getByLabel('Password', { exact: true }).fill(`local-${name}-password`);
  await page.getByRole('button', { name: 'Allow', exact: true }).click();
  await expect(page).toHaveURL(/\/apps$/);
}
async function createApp(page: Page, slug: string) {
  const known = (await (await page.request.get('/api/v1/apps')).json()).data.items;
  if (known.length) return known[0].applicationId as string;
  await page.getByRole('link', { name: 'Create app', exact: true }).click();
  await page.getByLabel('App name').fill(slug);
  await page.getByRole('button', { name: 'Create app', exact: true }).click();
  await expect(page).toHaveURL(/\/configuration$/);
  return new URL(page.url()).pathname.split('/')[2];
}
for (const [layout, viewport] of [
  ['desktop', { width: 1280, height: 960 }],
  ['mobile', { width: 390, height: 844 }],
] as const) {
  test(`shared app pages and task navigation · ${layout}`, async ({ browser }) => {
    test.setTimeout(180000);
    const contexts = await Promise.all(
      ['owner', 'staff', 'admin'].map(() =>
        browser.newContext({ viewport, ignoreHTTPSErrors: true }),
      ),
    );
    const [owner, staff, admin] = contexts;
    const [ownerPage, staffPage, page] = await Promise.all(
      contexts.map((context) => context.newPage()),
    );
    const failures: string[] = [];
    for (const target of [ownerPage, staffPage, page])
      target.on('response', (response) => {
        const path = new URL(response.url()).pathname;
        if (path.startsWith('/api/') && response.status() >= 400)
          failures.push(`${response.status()} ${path}`);
      });
    const suffix = crypto.randomUUID().replaceAll('-', '').slice(0, 8);
    async function shot(target: Page, role: string, name: string) {
      if (layout !== 'desktop') return;
      await expect(target.locator('.ui-skeleton')).toHaveCount(0);
      await target.evaluate(() => document.fonts.ready);
      await mkdir('/tmp/staff-admin-ui-shots', { recursive: true });
      await target.screenshot({
        path: `/tmp/staff-admin-ui-shots/${role}-${name}.png`,
        fullPage: true,
      });
    }
    try {
      for (const context of contexts) {
        await context.route('**/*', (route) => {
          const host = new URL(route.request().url()).hostname;
          return ['127.0.0.1', 'localhost'].includes(host)
            ? route.continue()
            : route.fulfill({
                status: 404,
                json: { message: 'Offline fixture' },
                headers: { 'Access-Control-Allow-Origin': '*' },
              });
        });
      }
      await page.bringToFront();
      await page.goto(bootstrapUrl());
      await page.getByLabel('Username', { exact: true }).fill('admin' + suffix);
      await page.getByLabel('New password', { exact: true }).fill(password);
      await page.getByRole('button', { name: 'Continue', exact: true }).click();
      const secret = await page.getByTestId('totp-secret').textContent();
      await page.getByLabel('Authentication code', { exact: true }).fill(code(secret!));
      await page.getByRole('button', { name: 'Finish setup', exact: true }).click();
      await expect(page).toHaveURL(/\/people$/);
      await expect(page.getByRole('heading', { name: 'People', exact: true })).toBeVisible();
      await page.getByRole('button', { name: 'Create account', exact: true }).click();
      const create = page.getByRole('dialog', { name: 'Create account', exact: true });
      await create.getByLabel('Username', { exact: true }).fill('staff' + suffix);
      await create.getByLabel('Display name').fill('Local Staff ' + suffix);
      await create.getByLabel('Role', { exact: true }).selectOption('staff');
      await create.getByRole('button', { name: 'Create account', exact: true }).click();
      const created = page.getByRole('dialog', { name: 'Account created', exact: true });
      const invitation = await created.getByLabel('Setup link', { exact: true }).inputValue();
      await created.getByRole('button', { name: 'Done', exact: true }).click();
      await staffPage.bringToFront();
      await staffPage.goto(invitation);
      await staffPage.getByLabel('New password', { exact: true }).fill(password);
      await staffPage.getByRole('button', { name: 'Continue', exact: true }).click();
      await staffPage.getByRole('button', { name: 'Finish setup', exact: true }).click();
      await expect(staffPage).toHaveURL(/\/apps$/);
      await commons(ownerPage, 'alice');
      const id = await createApp(ownerPage, 'review-' + suffix);
      const ownerSession = (await (await ownerPage.request.get('/api/v1/session')).json()).data;
      const headers = {
        'X-CSRF-Token': ownerSession.csrfToken,
        Origin: new URL(ownerPage.url()).origin,
      };
      let settings = (
        await (await ownerPage.request.get(`/api/v1/apps/${id}/configuration`)).json()
      ).data;
      if (!settings.revision) {
        const saved = await ownerPage.request.put(`/api/v1/apps/${id}/configuration`, {
          headers: { ...headers, 'Idempotency-Key': crypto.randomUUID() },
          data: {
            expectedRevision: 0,
            repository: 'https://github.com/example/student-app',
            branch: 'main',
            configuration: {
              schemaVersion: 1,
              build: { runtime: 'node', packages: ['.'], buildScript: null, startScript: 'start' },
              runtime: { port: 3000, healthPath: '/health' },
              storageBindings: [],
            },
          },
        });
        expect(saved.status()).toBe(200);
        settings = (await (await ownerPage.request.get(`/api/v1/apps/${id}/configuration`)).json())
          .data;
      }
      for (const [role, target] of [
        ['owner', ownerPage],
        ['staff', staffPage],
        ['admin', page],
      ] as const) {
        await target.bringToFront();
        await target.goto('/apps');
        await expect(target.getByRole('heading', { name: 'My apps', exact: true })).toBeVisible();
        await shot(target, role, 'my-apps');
        if (role !== 'owner') {
          const csrf = (await (await target.request.get('/api/v1/session')).json()).data.csrfToken;
          const read = await target.request.get(`/api/v1/apps/${id}`, {
            headers: { 'X-CSRF-Token': csrf },
          });
          expect(read.status()).toBe(200);
          expect((await read.json()).data.access).toBe('admin');
          await target.goto(`/apps/${id}`);
          for (const tab of ['Overview', 'Settings', 'Deploy', 'Deployments', 'Logs', 'Team']) {
            await target
              .getByRole('navigation', { name: 'App pages' })
              .getByRole('link', { name: tab, exact: true })
              .click();
            await expect(target.getByRole('navigation', { name: 'App pages' })).toBeVisible();
            await expect(target.locator('.ui-skeleton')).toHaveCount(0);
            await expect(
              target.getByRole('alert').filter({ hasText: /Couldn't load/ }),
            ).toHaveCount(0);
          }
        }
        if (role === 'owner') await target.goto(`/apps/${id}/configuration`);
        else
          await target
            .getByRole('navigation', { name: 'App pages' })
            .getByRole('link', { name: 'Settings', exact: true })
            .click();
        await expect(target.getByLabel('Build script')).toHaveValue('');
        await expect(target.getByLabel('Build script')).not.toHaveAttribute('placeholder', 'build');
        await expect(target.getByText('Leave empty if your app has no build step.')).toBeVisible();
        await shot(target, role, 'settings');
        if (role === 'owner')
          await expect(
            target.getByRole('button', { name: 'Change owner', exact: true }),
          ).toHaveCount(0);
        else
          await expect(
            target.getByRole('button', { name: 'Change owner', exact: true }),
          ).toBeVisible();
      }
      for (const [role, target] of [
        ['owner', ownerPage],
        ['staff', staffPage],
        ['admin', page],
      ] as const) {
        await target.bringToFront();
        await target.goto(`/apps/${id}/deploy`);
        await expect(target.getByLabel('Commit SHA')).toBeVisible();
        await expect(target.getByLabel('Sizing plan')).toHaveCount(0);
        if (role === 'owner') {
          await expect(target.getByLabel('Size', { exact: true })).toHaveCount(0);
          await expect(target.getByLabel('Builder size', { exact: true })).toHaveCount(0);
        } else {
          const size = target.getByLabel('Size', { exact: true });
          await expect(size).toHaveValue('');
          await expect(size.locator('option[value="200"]')).toHaveCount(1);
          await size.selectOption('200');
          await expect(target.getByText(/Changing size replaces the worker/)).toBeVisible();
          await expect(target.getByText(/The app gets up to/)).toBeVisible();
          await shot(target, role, 'worker-size');
          await size.selectOption('');
          const builder = target.getByLabel('Builder size', { exact: true });
          await expect(builder).toHaveValue('');
          await builder.selectOption('200');
          await target.getByRole('button', { name: 'Save builder size', exact: true }).click();
          await expect(target.getByText('Builder size saved.', { exact: true })).toBeVisible();
          await expect(builder).toHaveValue('200');
          await shot(target, role, 'builder-size');
          await builder.selectOption('');
          await target.getByRole('button', { name: 'Save builder size', exact: true }).click();
          await expect(builder).toHaveValue('');
          await expect(
            target.getByRole('button', { name: 'Save builder size', exact: true }),
          ).toBeDisabled();
        }
        if (role !== 'admin') {
          await target.goto('/platform-settings');
          await expect(
            target.getByRole('heading', { name: "You don't have access to this page" }),
          ).toBeVisible();
        }
      }
      await page.bringToFront();
      await page.goto('/platform-settings');
      const defaultBuilder = page.getByLabel('Default builder size', { exact: true });
      await expect(defaultBuilder).toHaveValue('50');
      await defaultBuilder.selectOption('200');
      await page.getByRole('button', { name: 'Save default builder size', exact: true }).click();
      await expect(page.getByText('Default builder size saved.', { exact: true })).toBeVisible();
      await expect(defaultBuilder).toHaveValue('200');
      await shot(page, 'admin', 'platform-settings');
      await defaultBuilder.selectOption('50');
      await page.getByRole('button', { name: 'Save default builder size', exact: true }).click();
      await expect(
        page.getByRole('button', { name: 'Save default builder size', exact: true }),
      ).toBeDisabled();
      const members = (await (await ownerPage.request.get(`/api/v1/apps/${id}/members`)).json())
        .data.items;
      expect(members).toHaveLength(1);
      expect(members[0].userId).toBe(ownerSession.user.id);
      for (const [role, target] of [
        ['staff', staffPage],
        ['admin', page],
      ] as const) {
        await ownerPage.request.post('/__test__/recovery-required', { headers, data: {} });
        const started = await ownerPage.request.post(`/api/v1/apps/${id}/deployments`, {
          headers: { ...headers, 'Idempotency-Key': crypto.randomUUID() },
          data: { configurationRevision: settings.revision, commit: 'a'.repeat(40) },
        });
        expect(started.status()).toBe(202);
        const intentId = (await started.json()).data.intentId;
        await expect
          .poll(
            async () =>
              (await (await ownerPage.request.get(`/api/v1/intents/${intentId}`)).json()).data
                .state,
          )
          .toBe('blocked');
        const intent = (await (await ownerPage.request.get(`/api/v1/intents/${intentId}`)).json())
          .data;
        // Each surface must expose exactly the same status and action.
        for (const [name, route] of [
          ['overview', `/apps/${id}`],
          ['deployments', `/apps/${id}/deployments`],
          ['deployment', `/apps/${id}/deployments/${intent.operationId}`],
          ['all-apps', '/all-apps'],
          ['people', '/people'],
          ['person', `/people/${ownerSession.user.id}`],
          ['activity', '/activity'],
        ] as const) {
          await target.bringToFront();
          await target.goto(route);
          if (name !== 'people') {
            await expect(
              target
                .locator('.ui-badge')
                .filter({ hasText: /^Needs attention$/ })
                .first(),
            ).toBeVisible();
            await expect(
              target.getByRole('button', { name: 'Resume', exact: true }).first(),
            ).toBeVisible();
          }
          if (name === 'overview') {
            await expect(target.getByRole('button', { name: 'Resume', exact: true })).toHaveCount(
              1,
            );
            await expect(
              target.getByText(/from Activity|Open Activity|previous change hasn’t finished/),
            ).toHaveCount(0);
          }
          await shot(target, role, name);
        }
        await ownerPage.bringToFront();
        await ownerPage.goto(`/apps/${id}`);
        await expect(
          ownerPage.getByRole('button', { name: 'Resume', exact: true }).first(),
        ).toBeVisible();
        await expect(ownerPage.getByRole('button', { name: 'Resume', exact: true })).toHaveCount(1);
        await shot(ownerPage, 'owner', 'overview');
        await ownerPage.bringToFront();
        await ownerPage.goto(`/apps/${id}/deployments`);
        await expect(
          ownerPage.getByRole('button', { name: 'Resume', exact: true }).first(),
        ).toBeVisible();
        await shot(ownerPage, 'owner', 'deployments');
        await target.getByRole('button', { name: 'Resume', exact: true }).first().click();
        await expect
          .poll(
            async () =>
              (await (await ownerPage.request.get(`/api/v1/intents/${intentId}`)).json()).data
                .state,
            { timeout: 15000 },
          )
          .toBe('succeeded');
      }
      await staffPage.bringToFront();
      await staffPage.goto('/people');
      await expect(
        staffPage.getByRole('button', { name: 'Create account', exact: true }),
      ).toHaveCount(0);
      await staffPage.bringToFront();
      await staffPage.goto(`/people/${ownerSession.user.id}`);
      await expect(
        staffPage.getByRole('heading', { name: 'Account controls', exact: true }),
      ).toHaveCount(0);
      await page.bringToFront();
      await page.goto(`/people/${ownerSession.user.id}`);
      await expect(
        page.getByRole('heading', { name: 'Account controls', exact: true }),
      ).toBeVisible();
      await shot(page, 'admin', 'person-controls');
      const controls = page.getByRole('region', { name: 'Account controls' });
      await controls.getByLabel('Apps', { exact: true }).fill('3');
      await controls.getByRole('button', { name: 'Save limits', exact: true }).click();
      await expect(page.getByText('Limits saved', { exact: true })).toBeVisible();
      for (const route of [`/api/v1/people/${ownerSession.user.id}/account`, '/api/v1/audit'])
        expect((await staffPage.request.get(route)).status()).toBe(403);
      await staffPage.bringToFront();
      await staffPage.goto('/audit');
      await expect(
        staffPage.getByRole('heading', { name: "You don't have access to this page" }),
      ).toBeVisible();
      await page.bringToFront();
      await page.goto('/audit');
      await expect(page.getByRole('heading', { name: 'Audit log', exact: true })).toBeVisible();
      await expect(page.getByText('Change resumed', { exact: true }).first()).toBeVisible();
      await shot(page, 'admin', 'audit');
      // Retired destinations are removed, with no redirects.
      for (const route of [
        '/staff/owners',
        '/staff/operations',
        '/admin/apps',
        '/admin/accounts',
        '/admin/audit',
      ]) {
        await page.bringToFront();
        await page.goto(route);
        await expect(
          page.getByRole('heading', { name: 'Page not found', exact: true }),
        ).toBeVisible();
        expect(new URL(page.url()).pathname).toBe(route);
      }
      for (const target of [ownerPage, staffPage, page])
        expect(
          await target.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
        ).toBe(true);
    } catch (error) {
      await mkdir('/tmp/staff-ui-browser-debug', { recursive: true });
      for (const [role, target] of [
        ['owner', ownerPage],
        ['staff', staffPage],
        ['admin', page],
      ] as const) {
        await writeFile(
          `/tmp/staff-ui-browser-debug/${layout}-${role}.txt`,
          await target
            .locator('main')
            .innerText({ timeout: 1000 })
            .catch(() => 'Page closed'),
        );
      }
      console.log(failures.join('\n'));
      throw error;
    } finally {
      await Promise.all(contexts.map((context) => context.close()));
    }
  });
}
