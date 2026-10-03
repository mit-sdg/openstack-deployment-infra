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
        if (!['127.0.0.1', 'localhost'].includes(url.hostname))
          unexpectedNetwork.push(url.hostname);
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
        await page.getByLabel('Application name').fill('student-project');
        await page.getByRole('button', { name: 'Create application', exact: true }).click();
        await expect(page).toHaveURL(/\/apps\/[a-f0-9-]+\/configuration$/);
        appId = new URL(page.url()).pathname.split('/')[2];
      } else await page.goto(`/apps/${appId}/configuration`);
      await page.getByLabel('Repository URL').fill('https://github.com/example/student-app');
      await page.getByLabel('Preferred branch').fill('main');
      await page.getByLabel('Bun', { exact: false }).check();
      await page.getByLabel('Start script').fill('start');
      await page.getByLabel('Application port').fill('3000');
      await page.getByLabel('Health path').fill('/health');
      await page.getByRole('button', { name: 'Save configuration' }).click();
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
      await page.evaluate(() => {
        if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
        window.scrollTo(0, 0);
      });
      await page.screenshot({
        path: path.join(screenshots, `${mode}-configuration.png`),
        fullPage: true,
      });
      await page.getByRole('link', { name: 'Deploy', exact: true }).click();
      const sha = crypto.randomUUID().replaceAll('-', '') + 'a'.repeat(8);
      await page.getByLabel('Full commit SHA').fill(sha);
      await page.getByRole('button', { name: 'Review deployment' }).click();
      await expect(page.getByRole('dialog')).toBeVisible();
      await expect(page.getByRole('dialog')).toContainText(sha);
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
      await page.getByRole('button', { name: 'Deploy this commit', exact: true }).click();
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
      ])
        expect((await bobPage.request.get(route)).status()).toBe(404);
      const bobApps = await bobPage.request.get('/api/v1/apps');
      const bobAppsBody = await bobApps.json();
      expect(bobApps.status(), bobAppsBody.error?.code).toBe(200);
      expect(bobAppsBody.data.items).toHaveLength(0);
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
