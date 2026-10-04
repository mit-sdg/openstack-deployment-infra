import { expect, test, type Page } from '@playwright/test';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';

const screenshots = path.resolve('../../.tmp/owner-portal-playwright/screenshots');
async function signIn(page: Page, owner: 'Alice' | 'Bob') {
  await page.goto('/sign-in');
  await page.getByLabel('Username', { exact: true }).fill(owner.toLowerCase());
  await page.getByLabel('Password', { exact: true }).fill(`local-${owner.toLowerCase()}-password`);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page).toHaveURL(/127\.0\.0\.1:\d+\/apps$/);
  await expect(page.getByRole('heading', { name: 'Apps', exact: true })).toBeVisible();
}

for (const [mode, viewport, colorScheme] of [
  ['desktop-light', { width: 1440, height: 1000 }, 'light'],
  ['desktop-dark', { width: 1440, height: 1000 }, 'dark'],
  ['mobile-light', { width: 390, height: 844 }, 'light'],
  ['mobile-dark', { width: 390, height: 844 }, 'dark'],
] as const) {
  test(`owner workflow · ${mode}`, async ({ browser }, testInfo) => {
    await mkdir(screenshots, { recursive: true });
    const context = await browser.newContext({ viewport, colorScheme, ignoreHTTPSErrors: true });
    const cspViolations: string[] = [];
    const unexpectedNetwork: string[] = [];
    const failedResponses: { method: string; path: string; status: number; error: unknown }[] = [];
    const diagnostics: Promise<void>[] = [];
    function capture(context: import('@playwright/test').BrowserContext) {
      context.on('response', (response) => {
        const url = new URL(response.url());
        if (!url.pathname.startsWith('/api/') || response.status() < 400) return;
        diagnostics.push(
          (async () => {
            const body = await response.json().catch(() => ({}));
            if (failedResponses.length < 24)
              failedResponses.push({
                method: response.request().method(),
                path: url.pathname,
                status: response.status(),
                error: {
                  code: body.error?.code ?? 'NON_JSON',
                  summary: body.error?.summary ?? '',
                  retryable: body.error?.retryable ?? false,
                },
              });
          })(),
        );
      });
    }
    capture(context);
    let passed = false;
    try {
      const page = await context.newPage();
      await context.addInitScript(() => {
        document.addEventListener('securitypolicyviolation', (event) => {
          console.error('PORTAL_CSP_VIOLATION:' + event.violatedDirective);
        });
      });
      context.on('page', (added) =>
        added.on('console', (message) => {
          if (message.text().includes('PORTAL_CSP_VIOLATION')) cspViolations.push('violation');
        }),
      );
      page.on('console', (message) => {
        if (message.text().includes('PORTAL_CSP_VIOLATION')) cspViolations.push('violation');
      });
      context.on('request', (request) => {
        const url = new URL(request.url());
        // GitHub's API is served by the route below and never reached.
        if (
          !['127.0.0.1', 'localhost', 'api.github.com', 'raw.githubusercontent.com'].includes(
            url.hostname,
          )
        )
          unexpectedNetwork.push(url.hostname);
      });
      // The Deploy page lists recent commits straight from GitHub's API; serve
      // them here, and check the browser sends no cookies or referrer there.
      const commits = [1, 2, 3].map((n) => ({
        sha: crypto.randomUUID().replaceAll('-', '') + String(n).repeat(8),
        commit: {
          message: `Fixture change ${n}\n\nBody`,
          author: { name: 'Fixture Author', date: new Date().toISOString() },
        },
      }));
      const github: import('@playwright/test').Request[] = [];
      const cors = { 'Access-Control-Allow-Origin': '*' };
      await context.route('https://api.github.com/**', (route) => {
        github.push(route.request());
        const url = route.request().url();
        if (!url.includes('/git/trees/')) return route.fulfill({ json: commits, headers: cors });
        // Fixture change 1 forgot its lockfile; the others are complete.
        const tree = [{ path: 'package.json', type: 'blob', mode: '100644', size: 40 }];
        if (!url.includes(commits[0].sha))
          tree.push({ path: 'bun.lock', type: 'blob', mode: '100644', size: 2 });
        return route.fulfill({ json: { tree, truncated: false }, headers: cors });
      });
      await context.route('https://raw.githubusercontent.com/**', (route) => {
        github.push(route.request());
        return route.fulfill({ body: '{"scripts":{"start":"node server.js"}}', headers: cors });
      });
      await page.goto('/sign-in');
      await page.getByLabel('Username', { exact: true }).fill('alice');
      await page.getByLabel('Password', { exact: true }).fill('incorrect-fixture');
      await page.getByRole('button', { name: 'Sign in', exact: true }).click();
      await expect(page.getByRole('alert')).toContainText('Username or password is incorrect.');
      await page.getByLabel('Username', { exact: true }).fill('carol');
      await page.getByLabel('Password', { exact: true }).fill('local-carol-password');
      await page.getByRole('button', { name: 'Sign in', exact: true }).click();
      await expect(page.getByRole('alert')).toContainText('disabled or archived');
      await page.evaluate(() => {
        if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
      });
      await page.screenshot({
        path: path.join(screenshots, `${mode}-sign-in.png`),
        fullPage: true,
      });
      await signIn(page, 'Alice');
      const aliceApps = await page.request.get('/api/v1/apps');
      const known = (await aliceApps.json()).data.items as {
        slug: string;
        applicationId: string;
      }[];
      // Re-runs reuse the Alice fixture app but do not reuse any authentication state.
      let appId =
        known.find((app) => app.slug === 'student-project')?.applicationId ??
        known[0]?.applicationId;
      if (!appId) {
        await page.getByRole('link', { name: 'Create app', exact: true }).click();
        await page.getByLabel('App name').fill('student-project');
        await page.getByRole('button', { name: 'Create app', exact: true }).click();
        await expect(page).toHaveURL(/\/apps\/[a-f0-9-]+\/configuration$/);
        appId = new URL(page.url()).pathname.split('/')[2];
      } else await page.goto(`/apps/${appId}/configuration`);
      await page.getByLabel('Repository URL').fill('https://github.com/example/student-app');
      await page.getByLabel('Branch', { exact: true }).fill('main');
      await page.getByLabel('Bun', { exact: false }).check();
      await page.getByLabel('Start script').fill('start');
      await page.getByLabel('Port', { exact: true }).fill('3000');
      await page.getByLabel('Health check path').fill('/health');
      await page.getByRole('button', { name: 'Save settings' }).click();
      await expect(page.getByRole('status').filter({ hasText: 'Settings saved.' })).toContainText(
        'Settings saved.',
      );
      await expect
        .poll(
          async () =>
            (await (await page.request.get(`/api/v1/apps/${appId}/configuration`)).json()).data
              .configuration.build.runtime,
        )
        .toBe('bun');
      // A private repository: the app gets a deploy key to add on GitHub.
      const access = page.getByRole('region', { name: 'Private repository' });
      // Layouts share one app, so later runs find the key already made.
      const create = access.getByRole('button', { name: 'Create deploy key' });
      await expect(create.or(access.getByLabel('Deploy key'))).toBeVisible();
      if (await create.isVisible()) await create.click();
      await expect(access.getByLabel('Deploy key')).toHaveValue(/^ssh-ed25519 /);
      await access.getByRole('button', { name: 'Check access' }).click();
      await expect(access.getByText('GitHub accepts the key. main is at 012345678.')).toBeVisible();
      await page.evaluate(() => {
        if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
        window.scrollTo(0, 0);
      });
      await page.screenshot({
        path: path.join(screenshots, `${mode}-configuration.png`),
        fullPage: true,
      });
      await page.getByRole('link', { name: 'Deploy', exact: true }).click();
      const sha = commits[1].sha;
      await page.getByRole('radio', { name: 'Fixture change 1' }).check();
      await expect(page.getByText('This commit will fail to build')).toBeVisible();
      await expect(page.getByText('Commit bun.lock in the repository root.')).toBeVisible();
      await page.getByRole('radio', { name: 'Fixture change 2' }).check();
      await expect(page.getByLabel('Commit SHA')).toHaveValue(sha);
      await expect(
        page.getByText('This commit has the package.json, scripts and lockfile the build needs.'),
      ).toBeVisible();
      expect(github.map((request) => request.url())).toEqual([
        'https://api.github.com/repos/example/student-app/commits?sha=main&per_page=5',
        `https://api.github.com/repos/example/student-app/git/trees/${commits[0].sha}?recursive=1`,
        `https://raw.githubusercontent.com/example/student-app/${commits[0].sha}/package.json`,
        `https://api.github.com/repos/example/student-app/git/trees/${sha}?recursive=1`,
        `https://raw.githubusercontent.com/example/student-app/${sha}/package.json`,
      ]);
      for (const request of github) {
        const headers = await request.allHeaders();
        expect(headers.cookie).toBeUndefined();
        expect(headers.referer).toBeUndefined();
      }
      await page.evaluate(() => {
        if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
        window.scrollTo(0, 0);
      });
      await page.screenshot({ path: path.join(screenshots, `${mode}-deploy.png`), fullPage: true });
      await page.getByRole('button', { name: 'Review deployment' }).click();
      await expect(page.getByRole('dialog')).toBeVisible();
      await expect(page.getByRole('dialog')).toContainText(sha);
      await expect(page.getByRole('dialog')).toContainText('Fixture change 2');
      await page.evaluate(() => {
        if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
        window.scrollTo(0, 0);
      });
      await page.screenshot({ path: path.join(screenshots, `${mode}-review.png`), fullPage: true });
      await page.evaluate(async () => {
        await fetch('/__test__/lost-response', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: '{}',
        });
      });
      await page.getByRole('dialog').getByRole('button', { name: 'Deploy', exact: true }).click();
      await expect(page.getByText('Deployment succeeded.', { exact: false })).toBeVisible({
        timeout: 30000,
      });
      const historyResponse = await page.request.get(`/api/v1/apps/${appId}/deployments`);
      const history = (await historyResponse.json()).data.items as {
        deploymentId: string;
        repositoryCommit: string;
      }[];
      const attempt = history.find((deployment) => deployment.repositoryCommit === sha)!;
      expect(history.filter((deployment) => deployment.repositoryCommit === sha)).toHaveLength(1);
      await page.goto(`/apps/${appId}/deployments/${attempt.deploymentId}`);
      await expect(page.getByRole('heading', { name: 'Build output' })).toBeVisible();
      await expect(page.getByLabel('Build log')).toContainText('Preparing exact source snapshot');
      await page.evaluate(() => {
        if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
        window.scrollTo(0, 0);
      });
      await page.screenshot({
        path: path.join(screenshots, `${mode}-build-log.png`),
        fullPage: true,
      });
      await page.goto(`/apps/${appId}/logs`);
      await expect(page.getByLabel('App output')).toContainText('Listening on port 3000');
      await page.evaluate(() => {
        if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
        window.scrollTo(0, 0);
      });
      await page.screenshot({ path: path.join(screenshots, `${mode}-logs.png`), fullPage: true });
      await page.getByRole('radio', { name: 'Errors' }).check();
      await expect(page.getByLabel('App errors')).toContainText('SESSION_SECRET');
      // A version that crashes on start is removed; its owner can see why.
      await page.evaluate(async () => {
        await fetch('/__test__/failed-deployment', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: '{}',
        });
      });
      await page.goto(`/apps/${appId}/deploy`);
      await page.getByRole('radio', { name: 'Fixture change 3' }).check();
      await page.getByRole('button', { name: 'Review deployment' }).click();
      await page.getByRole('dialog').getByRole('button', { name: 'Deploy', exact: true }).click();
      await page.getByRole('link', { name: 'See why it stopped' }).click({ timeout: 30000 });
      await expect(page.getByRole('heading', { name: 'Why it stopped' })).toBeVisible();
      await expect(
        page.getByText('Your app exited with code 1. It was restarted 3 times first.'),
      ).toBeVisible();
      await expect(page.getByLabel('Startup errors')).toContainText("Cannot find module 'express'");
      await page.evaluate(() => {
        if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
        window.scrollTo(0, 0);
      });
      await page.screenshot({
        path: path.join(screenshots, `${mode}-why-stopped.png`),
        fullPage: true,
      });
      await page.goto('/apps');
      await expect(
        page
          .getByRole('row')
          .filter({ has: page.locator(`a[href="/apps/${appId}"]`) })
          .getByText('Healthy', { exact: true }),
      ).toBeVisible();
      await page.evaluate(() => {
        if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
        window.scrollTo(0, 0);
      });
      await page.screenshot({
        path: path.join(screenshots, `${mode}-dashboard.png`),
        fullPage: true,
      });
      expect(
        await page.locator('body').evaluate((body) => body.scrollWidth <= window.innerWidth),
      ).toBe(true);
      const secure = testInfo.project.use.baseURL!.startsWith('https:');
      const cookies = await context.cookies(testInfo.project.use.baseURL!);
      const session = cookies.find(
        (cookie) => cookie.name === (secure ? '__Host-portal-session' : 'portal-dev-session'),
      );
      expect(
        !!session &&
          session.secure === secure &&
          session.httpOnly &&
          session.sameSite === 'Lax' &&
          session.path === '/',
      ).toBe(true);
      const bob = await browser.newContext({ viewport, colorScheme, ignoreHTTPSErrors: true });
      capture(bob);
      await bob.addInitScript(() => {
        document.addEventListener('securitypolicyviolation', (event) => {
          console.error('PORTAL_CSP_VIOLATION:' + event.violatedDirective);
        });
      });
      bob.on('page', (added) =>
        added.on('console', (message) => {
          if (message.text().includes('PORTAL_CSP_VIOLATION')) cspViolations.push('violation');
        }),
      );
      bob.on('request', (request) => {
        const url = new URL(request.url());
        if (!['127.0.0.1', 'localhost'].includes(url.hostname))
          unexpectedNetwork.push(url.hostname);
      });
      const bobPage = await bob.newPage();
      await signIn(bobPage, 'Bob');
      for (const route of [
        `/api/v1/apps/${appId}`,
        `/api/v1/apps/${appId}/configuration`,
        `/api/v1/apps/${appId}/deployments/${attempt.deploymentId}`,
        `/api/v1/apps/${appId}/deployments/${attempt.deploymentId}/build-log`,
        `/api/v1/apps/${appId}/logs`,
        `/api/v1/apps/${appId}/deployments/${attempt.deploymentId}/startup-log`,
      ])
        expect((await bobPage.request.get(route)).status()).toBe(404);
      const bobApps = await bobPage.request.get('/api/v1/apps');
      const bobAppsBody = await bobApps.json();
      expect(bobApps.status(), bobAppsBody.error?.code).toBe(200);
      expect(bobAppsBody.data.items).toHaveLength(0);
      // Alice adds Bob to the app's team; he can then work on it, and leave.
      await page.goto(`/apps/${appId}/team`);
      const team = page.getByRole('region', { name: 'Team' });
      await team.getByLabel('Add by username').fill('bob');
      await team.getByRole('button', { name: 'Add to team' }).click();
      await expect(team.getByRole('table', { name: 'Team' })).toContainText('Bob Student');
      await page.evaluate(() => {
        if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
        window.scrollTo(0, 0);
      });
      await page.screenshot({ path: path.join(screenshots, `${mode}-team.png`), fullPage: true });
      await bobPage.goto('/apps');
      await expect(bobPage.getByText('Alice Student’s app')).toBeVisible();
      expect((await bobPage.request.get(`/api/v1/apps/${appId}/logs`)).status()).toBe(200);
      await bobPage.goto(`/apps/${appId}/team`);
      await bobPage.getByRole('button', { name: 'Leave', exact: true }).click();
      await bobPage
        .getByRole('dialog', { name: 'Leave this app?' })
        .getByRole('button', { name: 'Leave app' })
        .click();
      await expect(bobPage).toHaveURL(/\/apps$/);
      expect((await bobPage.request.get(`/api/v1/apps/${appId}`)).status()).toBe(404);
      await bobPage.getByRole('button', { name: /^Account: / }).click();
      await bobPage.getByRole('button', { name: 'Sign out' }).click();
      await expect(bobPage.getByRole('button', { name: 'Sign in', exact: true })).toBeVisible();
      await bob.close();
      await page.getByRole('button', { name: /^Account: / }).click();
      await page.getByRole('button', { name: 'Sign out' }).click();
      await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toBeVisible();
      expect((await page.request.get('/api/v1/apps')).status()).toBe(401);
      expect(cspViolations).toHaveLength(0);
      expect(unexpectedNetwork).toHaveLength(0);
      await context.close();
      passed = true;
    } finally {
      await Promise.all(diagnostics);
      if (!passed || testInfo.status !== testInfo.expectedStatus) {
        const directory = path.resolve('../../.tmp/owner-portal-playwright/diagnostics');
        await mkdir(directory, { recursive: true });
        await writeFile(
          path.join(directory, `${mode}.json`),
          JSON.stringify({ version: 1, failures: failedResponses }, null, 2).slice(0, 8192),
        );
      }
      await context.close();
    }
  });
}
