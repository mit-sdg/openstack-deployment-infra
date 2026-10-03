import { expect, test } from '@playwright/test';

test('owner environment, PostgreSQL bindings, deploy names and rotation', async ({ page }) => {
  await page.goto('/sign-in');
  await page.getByLabel('Username', { exact: true }).fill('alice');
  await page.getByLabel('Password', { exact: true }).fill('local-alice-password');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page).toHaveURL(/\/apps$/);
  const apps = (await (await page.request.get('/api/v1/apps')).json()).data.items;
  const existing = apps.find((app: { slug: string }) => app.slug === 'resource-project');
  let id = existing?.applicationId;
  if (!id) {
    await page.goto('/apps/new');
    await page.getByLabel('Application name').fill('resource-project');
    await page.getByRole('button', { name: 'Create application', exact: true }).click();
    await expect(page).toHaveURL(/\/configuration$/);
    id = new URL(page.url()).pathname.split('/')[2];
  } else await page.goto(`/apps/${id}/configuration`);
  await page.getByLabel('Repository URL').fill('https://github.com/example/app');
  const sentinel = 'WRITE_ONLY_SENTINEL_784f';
  const received: string[] = [];
  const pending: Promise<void>[] = [];
  page.on('response', (response) => {
    if (new URL(response.url()).pathname.startsWith('/api/'))
      pending.push(
        response
          .text()
          .then((body) => {
            received.push(body);
          })
          .catch(() => {}),
      );
  });
  await page.getByLabel('Variable name').fill('API_TOKEN');
  await page.getByLabel('New value').fill(sentinel);
  await page.getByRole('button', { name: 'Add or replace variable' }).click();
  await expect(page.getByLabel('New value')).toHaveValue('');
  await expect
    .poll(async () =>
      (await (await page.request.get(`/api/v1/apps/${id}/environment`)).json()).data.items.map(
        (item: { name: string }) => item.name,
      ),
    )
    .toContain('API_TOKEN');
  const add = page.getByRole('button', { name: 'Add PostgreSQL', exact: true });
  if (await add.isEnabled()) await add.click();
  const bindings = page.getByRole('group', { name: 'PostgreSQL bindings' });
  await expect(bindings).toBeVisible();
  const defaults = page.getByRole('button', { name: 'Use default bindings' });
  if (await defaults.isVisible()) await defaults.click();
  await bindings.getByLabel('url → environment name').fill('APP_DATABASE');
  const host = bindings.getByRole('button', { name: 'Remove host binding' });
  if (await host.isVisible()) await host.click();
  await expect(page.getByRole('button', { name: 'Verify PostgreSQL' })).toBeEnabled({
    timeout: 15000,
  });
  await expect(add).toBeDisabled();
  const storageSection = page
    .locator('section')
    .filter({ has: page.getByRole('heading', { name: 'Databases and storage', exact: true }) })
    .first();
  await expect(storageSection.getByRole('button', { name: /delete/i })).toHaveCount(0);
  await expect(page.getByText(/Ask an administrator to delete/)).toBeVisible();
  await page.getByRole('button', { name: 'Save configuration' }).click();
  await expect(page.getByText('Settings saved.', { exact: false })).toBeVisible();
  await page.goto(`/apps/${id}/deploy`);
  await expect(page.getByText('APP_DATABASE', { exact: true })).toBeVisible();
  await expect(page.getByText('API_TOKEN', { exact: true })).toBeVisible();
  await expect(page.getByText('PGHOST', { exact: true })).toHaveCount(0);
  await page.getByLabel('Full commit SHA').fill('d'.repeat(40));
  await page.getByRole('button', { name: 'Review deployment →' }).click();
  await expect(page.getByRole('dialog')).toContainText('APP_DATABASE');
  await page.getByRole('button', { name: 'Deploy this commit', exact: true }).click();
  await expect(page.getByText('Deployment succeeded.', { exact: false })).toBeVisible({
    timeout: 15000,
  });
  await page.goto(`/apps/${id}/configuration`);
  let warning = '';
  page.once('dialog', async (dialog) => {
    warning = dialog.message();
    await dialog.accept();
  });
  await page.getByRole('button', { name: 'Rotate PostgreSQL credentials' }).click();
  expect(warning).toContain('Redeploy');
  await expect(page.getByText('Rotate storage credentials', { exact: true })).toBeVisible();
  await expect
    .poll(
      async () =>
        (await (await page.request.get(`/api/v1/apps/${id}`)).json()).data.configurationChanged,
      { timeout: 15000 },
    )
    .toBe(true);
  await Promise.all(pending);
  expect(received.join('')).not.toContain(sentinel);
});
