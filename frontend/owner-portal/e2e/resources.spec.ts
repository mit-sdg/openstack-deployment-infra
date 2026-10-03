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
    await page.getByLabel('App name').fill('resource-project');
    await page.getByRole('button', { name: 'Create app', exact: true }).click();
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
  // Recent changes sit in their own list, apart from the variables and resources.
  const operationRow = (section: Locator, createdAt: string) =>
    section
      .getByRole('list', { name: /changes$/ })
      .getByRole('listitem')
      .filter({ has: page.locator(`time[datetime="${createdAt}"]`) });
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
  ).toBeAttached({ timeout: operationTimeout });
  const variables = environmentSection.getByRole('list', { name: 'Environment variables' });
  await expect(
    variables.getByRole('listitem').filter({ has: page.getByText('API_TOKEN', { exact: true }) }),
  ).toBeVisible({ timeout: operationTimeout });

  await page.getByRole('button', { name: 'Save settings' }).click();
  await expect(page.getByText('Settings saved.', { exact: false })).toBeVisible();

  // A disabled button can mean a pending env observation, not existing storage.
  // Read existence once, then wait for the UI's actual action/readiness states.
  const storageResponse = await page.request.get(`/api/v1/apps/${id}/storage`);
  expect(storageResponse.ok()).toBe(true);
  const existingPostgres = (await storageResponse.json()).data.items.find(
    (item: { type: string }) => item.type === 'postgres',
  );
  const add = page.getByRole('button', { name: 'Add PostgreSQL', exact: true });
  const bindings = page.getByRole('group', { name: 'PostgreSQL bindings' });
  const postgresStatus = storageSection
    .getByRole('listitem')
    .filter({ hasText: 'PostgreSQL' })
    .locator('.ui-badge')
    .first();
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
      await expect(postgresStatus).toHaveText('Setting up', { timeout: operationTimeout });
      await expect(add).toHaveCount(0);
    } finally {
      const released = await page.request.post('/__test__/finish-storage-creation', {
        headers: { Origin: new URL(page.url()).origin },
        data: {},
      });
      expect(released.ok()).toBe(true);
    }
    await expect(
      operationRow(storageSection, createIntent.createdAt).getByText('Succeeded', { exact: true }),
    ).toBeAttached({ timeout: operationTimeout });
  }
  await expect(postgresStatus).toHaveText('Ready', { timeout: operationTimeout });
  await expect(page.getByRole('button', { name: 'Verify PostgreSQL' })).toBeEnabled({
    timeout: operationTimeout,
  });
  await expect(add).toHaveCount(0);
  // Adding a database opens its variables prefilled with defaults; otherwise
  // open the editor from the row. Reset so reruns start from the same state.
  const editor = page.getByRole('dialog');
  if (!(await editor.isVisible())) {
    const edit = page.getByRole('button', { name: 'Edit PostgreSQL variables' });
    if (await edit.count()) await edit.click();
    else await page.getByRole('button', { name: 'Choose PostgreSQL variable names' }).click();
  }
  await editor.getByRole('button', { name: 'Reset to defaults' }).click();
  await bindings.getByLabel('url → environment name').fill('APP_DATABASE');
  await bindings.getByRole('button', { name: 'Remove host' }).click();
  await expect(bindings.getByRole('button', { name: 'Add host as PGHOST' })).toBeVisible();
  // Saving the names saves settings straight away.
  await editor.getByRole('button', { name: 'Save variables' }).click();
  await expect(editor).toHaveCount(0);
  await expect
    .poll(
      async () =>
        JSON.stringify(
          (await (await page.request.get(`/api/v1/apps/${id}/configuration`)).json()).data
            .configuration.storageBindings,
        ),
      { timeout: operationTimeout },
    )
    .toContain('"url":"APP_DATABASE"');
  await expect(storageSection.getByRole('button', { name: /delete/i })).toHaveCount(0);
  await expect(page.getByText(/Only an admin can delete/)).toBeVisible();
  await page.goto(`/apps/${id}/deploy`);
  await expect(page.getByText('APP_DATABASE', { exact: true })).toBeVisible();
  await expect(page.getByText('API_TOKEN', { exact: true })).toBeVisible();
  await expect(page.getByText('PGHOST', { exact: true })).toHaveCount(0);
  await page.getByLabel('Commit SHA').fill('d'.repeat(40));
  await page.getByRole('button', { name: 'Review deployment' }).click();
  await expect(page.getByRole('dialog')).toContainText('APP_DATABASE');
  await page.getByRole('dialog').getByRole('button', { name: 'Deploy', exact: true }).click();
  await expect(page.getByText('Deployment succeeded.', { exact: false })).toBeVisible({
    timeout: operationTimeout,
  });
  await page.goto(`/apps/${id}/configuration`);
  // Native prompts are gone: any window.confirm would fail this test.
  page.on('dialog', (dialog) => {
    throw new Error(`Unexpected native dialog: ${dialog.message()}`);
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
  await expect(page.getByRole('dialog')).toContainText('next deploy');
  await page.getByRole('dialog').getByRole('button', { name: 'Rotate credentials' }).click();
  const rotation = (await (await rotated).json()).data;
  await expect(
    operationRow(storageSection, rotation.createdAt).getByText('Succeeded', { exact: true }),
  ).toBeAttached({ timeout: operationTimeout });
  await expect(page.getByRole('button', { name: 'Rotate PostgreSQL credentials' })).toBeEnabled({
    timeout: operationTimeout,
  });
  await expect(operationRow(storageSection, rotation.createdAt)).toContainText(
    'Credentials rotated',
  );
  await page.goto(`/apps/${id}`);
  await expect(
    page.getByText('Settings changed since the last deploy.', { exact: false }),
  ).toBeVisible({ timeout: operationTimeout });
  await page.goto(`/apps/${id}/configuration`);
  await expect(page.getByRole('button', { name: 'Delete API_TOKEN' })).toBeEnabled({
    timeout: operationTimeout,
  });
  const deleted = page.waitForResponse(
    (response) =>
      response.request().method() === 'DELETE' &&
      new URL(response.url()).pathname.endsWith('/environment/API_TOKEN') &&
      response.ok(),
  );
  await page.getByRole('button', { name: 'Delete API_TOKEN' }).click();
  await expect(page.getByRole('dialog')).toContainText('Delete API_TOKEN?');
  await page.getByRole('dialog').getByRole('button', { name: 'Delete variable' }).click();
  const deleteIntent = (await (await deleted).json()).data;
  await expect(
    operationRow(environmentSection, deleteIntent.createdAt).getByText('Succeeded', {
      exact: true,
    }),
  ).toBeAttached({ timeout: operationTimeout });
  await expect(
    variables.getByRole('listitem').filter({ has: page.getByText('API_TOKEN', { exact: true }) }),
  ).toHaveCount(0, { timeout: operationTimeout });
  await Promise.all(pending);
  expect(received.join('')).not.toContain(sentinel);
});
