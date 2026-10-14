import { createApp, expect, saveSettings, signIn, test } from './helpers';

test.use({ viewport: { width: 1440, height: 1000 }, colorScheme: 'light' });

test('S3 defaults are saved explicitly and existing names change only after a saved reset', async ({
  page,
}) => {
  await signIn(page, 'bob');
  const id = await createApp(page, 's3-names-project');
  await saveSettings(page);
  await page.getByRole('button', { name: 'Add S3 storage', exact: true }).click();
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Add S3 storage', exact: true })
    .click();
  const dialog = page.getByRole('dialog', { name: 'S3 storage variables' });
  const defaults = {
    endpoint: 'S3_ENDPOINT',
    public_endpoint: 'S3_PUBLIC_ENDPOINT',
    region: 'S3_REGION',
    access_key_id: 'S3_ACCESS_KEY_ID',
    secret_access_key: 'S3_SECRET_ACCESS_KEY',
    bucket: 'S3_BUCKET',
  };
  for (const [output, name] of Object.entries(defaults))
    await expect(dialog.getByLabel(`${output} → environment name`, { exact: true })).toHaveValue(
      name,
    );
  await dialog.getByRole('button', { name: 'Save variables' }).click();
  await expect(dialog).toHaveCount(0);
  async function savedOutputs() {
    const response = await page.request.get(`/api/v1/apps/${id}/configuration`);
    expect(response.ok()).toBe(true);
    return (await response.json()).data.configuration.storageBindings[0].outputs;
  }
  expect(await savedOutputs()).toEqual(defaults);
  await page.getByRole('button', { name: 'Edit S3 storage variables' }).click();
  const savedNames = {
    ...defaults,
    endpoint: 'AWS_ENDPOINT_URL_S3',
    region: 'AWS_REGION',
    access_key_id: 'AWS_ACCESS_KEY_ID',
    secret_access_key: 'AWS_SECRET_ACCESS_KEY',
  };
  for (const [output, name] of Object.entries(savedNames))
    await dialog.getByLabel(`${output} → environment name`, { exact: true }).fill(name);
  await dialog.getByRole('button', { name: 'Save variables' }).click();
  await expect(dialog).toHaveCount(0);
  await page.reload();
  await page.getByRole('button', { name: 'Edit S3 storage variables' }).click();
  for (const [output, name] of Object.entries(savedNames))
    await expect(dialog.getByLabel(`${output} → environment name`, { exact: true })).toHaveValue(
      name,
    );
  await dialog.getByRole('button', { name: 'Reset to defaults' }).click();
  for (const [output, name] of Object.entries(defaults))
    await expect(dialog.getByLabel(`${output} → environment name`, { exact: true })).toHaveValue(
      name,
    );
  expect(await savedOutputs()).toEqual(savedNames);
  await dialog.getByRole('button', { name: 'Save variables' }).click();
  await expect(dialog).toHaveCount(0);
  expect(await savedOutputs()).toEqual(defaults);
});
