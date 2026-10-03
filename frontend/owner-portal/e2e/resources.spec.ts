import { expect, test, type Locator } from '@playwright/test';

const operationTimeout = 30_000;

test('owner environment, PostgreSQL bindings, deploy names and rotation', async ({ page }) => {
  test.setTimeout(180_000);
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
  const environmentSection = page
    .locator('section')
    .filter({ has: page.getByRole('heading', { name: 'Environment variables', exact: true }) });
  const storageSection = page
    .locator('section')
    .filter({ has: page.getByRole('heading', { name: 'Databases and storage', exact: true }) });
  const operationRow = (section: Locator, createdAt: string) =>
    section.getByRole('listitem').filter({ has: page.locator(`time[datetime="${createdAt}"]`) });
  await page.getByLabel('Variable name').fill('API_TOKEN');
  await page.getByLabel('New value').fill(sentinel);
  const written = page.waitForResponse(
    (response) =>
      response.request().method() === 'PUT' &&
      new URL(response.url()).pathname.endsWith('/environment/API_TOKEN') &&
      response.ok(),
  );
  await page.getByRole('button', { name: 'Add or replace variable' }).click();
  const writeIntent = (await (await written).json()).data;
  await expect(page.getByLabel('New value')).toHaveValue('');
  await expect(
    operationRow(environmentSection, writeIntent.createdAt).getByText('Succeeded', { exact: true }),
  ).toBeVisible({ timeout: operationTimeout });
  await expect(
    environmentSection
      .getByRole('row')
      .filter({ has: page.getByText('API_TOKEN', { exact: true }) }),
  ).toBeVisible({ timeout: operationTimeout });

  // A disabled button can mean a pending env observation, not existing storage.
  // Read existence once, then wait for the UI's actual action/readiness states.
  const storageResponse = await page.request.get(`/api/v1/apps/${id}/storage`);
  expect(storageResponse.ok()).toBe(true);
  const existingPostgres = (await storageResponse.json()).data.items.find(
    (item: { type: string }) => item.type === 'postgres',
  );
  const add = page.getByRole('button', { name: 'Add PostgreSQL', exact: true });
  const bindings = page.getByRole('group', { name: 'PostgreSQL bindings' });
  const postgresStatus = bindings.locator('..').locator('h3 .chip');
  if (!existingPostgres) {
    await expect(add).toBeEnabled({ timeout: operationTimeout });
    // Hold controller completion until the real provisioning chip is visible;
    // releasing the dev-only gate completes without a wall-clock delay.
    const paused = await page.request.post('/__test__/pause-storage-creation', {
      headers: { Origin: new URL(page.url()).origin },
      data: {},
    });
    expect(paused.ok()).toBe(true);
    let createIntent: { createdAt: string };
    try {
      const created = page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          new URL(response.url()).pathname === `/api/v1/apps/${id}/storage` &&
          response.ok(),
      );
      await add.click();
      createIntent = (await (await created).json()).data;
      await expect(postgresStatus).toHaveText('provisioning', { timeout: operationTimeout });
      await expect(add).toBeDisabled();
    } finally {
      const released = await page.request.post('/__test__/finish-storage-creation', {
        headers: { Origin: new URL(page.url()).origin },
        data: {},
      });
      expect(released.ok()).toBe(true);
    }
    await expect(
      operationRow(storageSection, createIntent.createdAt).getByText('Succeeded', { exact: true }),
    ).toBeVisible({ timeout: operationTimeout });
  }
  await expect(postgresStatus).toHaveText('ready', { timeout: operationTimeout });
  await expect(page.getByRole('button', { name: 'Verify PostgreSQL' })).toBeEnabled({
    timeout: operationTimeout,
  });
  await expect(add).toBeDisabled();
  // Fresh creation seeds defaults; a retained fixture may have saved a subset.
  const saved = (await (await page.request.get(`/api/v1/apps/${id}/configuration`)).json()).data
    .configuration.storageBindings;
  const initialOutputs = existingPostgres
    ? (saved.find((item: { resourceId: string }) => item.resourceId === existingPostgres.resourceId)
        ?.outputs ?? {})
    : { url: 'DATABASE_URL', host: 'PGHOST' };
  if (existingPostgres && !Object.keys(initialOutputs).length) {
    await page.getByRole('button', { name: 'Use default bindings' }).click();
  } else if (existingPostgres && !('url' in initialOutputs)) {
    await bindings.getByRole('button', { name: 'Bind url to DATABASE_URL' }).click();
  }
  await bindings.getByLabel('url → environment name').fill('APP_DATABASE');
  if (!existingPostgres || !Object.keys(initialOutputs).length || 'host' in initialOutputs) {
    await bindings.getByRole('button', { name: 'Remove host binding' }).click();
  }
  await expect(bindings.getByRole('button', { name: 'Bind host to PGHOST' })).toBeVisible();
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
    timeout: operationTimeout,
  });
  await page.goto(`/apps/${id}/configuration`);
  let warning = '';
  page.once('dialog', async (dialog) => {
    warning = dialog.message();
    await dialog.accept();
  });
  const rotated = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      new URL(response.url()).pathname.endsWith('/rotate') &&
      response.ok(),
  );
  await expect(page.getByRole('button', { name: 'Rotate PostgreSQL credentials' })).toBeEnabled({
    timeout: operationTimeout,
  });
  await page.getByRole('button', { name: 'Rotate PostgreSQL credentials' }).click();
  const rotation = (await (await rotated).json()).data;
  await expect(
    operationRow(storageSection, rotation.createdAt).getByText('Succeeded', { exact: true }),
  ).toBeVisible({ timeout: operationTimeout });
  await expect(page.getByRole('button', { name: 'Rotate PostgreSQL credentials' })).toBeEnabled({
    timeout: operationTimeout,
  });
  expect(warning).toContain('Redeploy');
  await expect(operationRow(storageSection, rotation.createdAt)).toContainText(
    'Rotate storage credentials',
  );
  await page.goto(`/apps/${id}`);
  await expect(
    page.getByText('Configuration changed since last deploy.', { exact: false }),
  ).toBeVisible({ timeout: operationTimeout });
  await page.goto(`/apps/${id}/configuration`);
  await expect(page.getByRole('button', { name: 'Delete API_TOKEN' })).toBeEnabled({
    timeout: operationTimeout,
  });
  page.once('dialog', (dialog) => dialog.accept());
  const deleted = page.waitForResponse(
    (response) =>
      response.request().method() === 'DELETE' &&
      new URL(response.url()).pathname.endsWith('/environment/API_TOKEN') &&
      response.ok(),
  );
  await page.getByRole('button', { name: 'Delete API_TOKEN' }).click();
  const deleteIntent = (await (await deleted).json()).data;
  await expect(
    operationRow(environmentSection, deleteIntent.createdAt).getByText('Succeeded', {
      exact: true,
    }),
  ).toBeVisible({ timeout: operationTimeout });
  await expect(
    environmentSection
      .getByRole('row')
      .filter({ has: page.getByText('API_TOKEN', { exact: true }) }),
  ).toHaveCount(0, { timeout: operationTimeout });
  await Promise.all(pending);
  expect(received.join('')).not.toContain(sentinel);
});
