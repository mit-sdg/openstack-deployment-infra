import { createApp, expect, saveSettings, signIn, test } from './helpers';

test.use({ viewport: { width: 390, height: 844 } });

test('owner saves a write-only environment variable and provisions PostgreSQL bindings', async ({
  page,
}) => {
  await signIn(page, 'alice');
  const id = await createApp(page, 'resource-project');
  await saveSettings(page);
  const sentinel = 'WRITE_ONLY_SENTINEL_784f';
  await page.getByLabel('Variable name').fill('API_TOKEN');
  await page.getByLabel('New value').fill(sentinel);
  await page.getByRole('button', { name: 'Save variable', exact: true }).click();
  await expect(page.getByLabel('New value')).toHaveValue('');
  const variables = page.getByRole('list', { name: 'Environment variables' });
  await expect(variables).toContainText('API_TOKEN');
  const response = await page.request.get(`/api/v1/apps/${id}/environment`);
  expect(response.ok()).toBe(true);
  expect(await response.text()).not.toContain(sentinel);

  await page.getByRole('button', { name: 'Add PostgreSQL', exact: true }).click();
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Add PostgreSQL', exact: true })
    .click();
  const editor = page.getByRole('dialog');
  await editor
    .getByRole('group', { name: 'PostgreSQL bindings' })
    .getByLabel('url → environment name')
    .fill('APP_DATABASE');
  await editor.getByRole('button', { name: 'Save variables' }).click();
  await expect(editor).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Verify PostgreSQL' })).toBeEnabled();

  // Reload proves the real server persisted both the variable and binding.
  await page.reload();
  await expect(variables).toContainText('API_TOKEN');
  await expect(page.getByRole('button', { name: 'Verify PostgreSQL' })).toBeEnabled();
  await page.goto(`/apps/${id}/deploy`);
  await expect(page.getByText('APP_DATABASE', { exact: true })).toBeVisible();
  await expect(page.getByText('API_TOKEN', { exact: true })).toBeVisible();
});
