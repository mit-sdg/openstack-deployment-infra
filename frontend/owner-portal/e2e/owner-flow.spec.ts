import { createApp, expect, saveSettings, signIn, test } from './helpers';

test('owner signs in, creates and deploys an app, reads output and signs out', async ({ page }) => {
  await signIn(page, 'alice');
  const id = await createApp(page, 'student-project');
  await saveSettings(page);

  const access = page.getByRole('region', { name: 'Private repository' });
  await access.getByRole('button', { name: 'Create deploy key' }).click();
  await expect(access.getByLabel('Deploy key')).toHaveValue(/^ssh-ed25519 /);
  await access.getByRole('button', { name: 'Check access' }).click();
  await expect(access.getByText(/GitHub accepts the key/)).toBeVisible();

  await page.getByRole('link', { name: 'Deploy', exact: true }).click();
  await page.getByRole('radio', { name: 'Private repository fixture' }).check();
  await page.getByRole('button', { name: 'Review deployment' }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Deploy', exact: true }).click();
  await expect(page.getByText('Deployment succeeded.', { exact: false })).toBeVisible();

  await page.goto(`/apps/${id}/deployments`);
  await page
    .getByRole('table', { name: 'Deployments' })
    .getByRole('link', { name: '012345678', exact: true })
    .click();
  await expect(page.getByLabel('Build log')).toContainText('Preparing exact source snapshot');
  await page.goto(`/apps/${id}/logs`);
  await expect(page.getByLabel('App output')).toContainText('Listening on port 3000');
  await page.getByRole('button', { name: /^Account: / }).click();
  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByRole('link', { name: 'Sign in with your class account' })).toBeVisible();
  expect((await page.request.get('/api/v1/apps')).status()).toBe(401);
});
