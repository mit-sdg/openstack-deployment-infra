import { createApp, expect, signIn, test } from './helpers';

test.use({ colorScheme: 'dark' });

test('staff finds an owner app and opens its settings, people and activity', async ({ page }) => {
  await signIn(page, 'bob');
  const id = await createApp(page, 'staff-review');
  const ownerId = (await (await page.request.get('/api/v1/session')).json()).data.user.id;
  await page.getByRole('button', { name: /^Account: / }).click();
  await page.getByRole('button', { name: 'Sign out' }).click();

  // Taylor is enrolled as staff by the harness, using the real class sign-in.
  await signIn(page, 'taylor');
  await page.getByRole('link', { name: 'All apps', exact: true }).click();
  const table = page.getByRole('table', { name: 'All apps' });
  await expect(table).toContainText('staff-review');
  await table.getByRole('link', { name: 'staff-review', exact: true }).click();
  await expect(page.getByRole('navigation', { name: 'App pages' })).toBeVisible();
  await page
    .getByRole('navigation', { name: 'App pages' })
    .getByRole('link', { name: 'Settings' })
    .click();
  await expect(page.getByLabel('Repository URL')).toBeVisible();
  const builder = page.getByLabel('Build machine', { exact: true });
  await builder.selectOption('200');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByText('Build machine saved.', { exact: true })).toBeVisible();

  await page.getByRole('link', { name: 'People', exact: true }).click();
  await expect(page.getByRole('table')).toContainText('Bob Student');
  await page.goto(`/people/${ownerId}`);
  await expect(page.getByRole('main')).toContainText('staff-review');
  await page.getByRole('link', { name: 'Activity', exact: true }).click();
  await expect(page.getByRole('main')).toContainText('staff-review');
  expect((await page.request.get('/api/v1/audit')).status()).toBe(403);
});
