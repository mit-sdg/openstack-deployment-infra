import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { adminApi } from './adminApi';
import { api, clearCredentials, type Session } from './api';

// Nested staff and admin routes must reach their pages. Page data never
// resolves: the test asserts routing (the right request, no "Page not
// found"), not page markup.
const owner = '22222222-2222-4222-8222-222222222222';
const app = '33333333-3333-4333-8333-333333333333';
const deployment = '44444444-4444-4444-8444-444444444444';
const pending = () => new Promise<never>(() => {});
const session = (role: Session['role']): Session => ({
  role,
  stepUpExpiresAt: null,
  csrfToken: 'csrf',
  platformName: 'Example platform',
  expiresAt: new Date(Date.now() + 3600000).toISOString(),
  user: { id: '11111111-1111-4111-8111-111111111111', username: 'taylor', displayName: 'Taylor' },
  quota: {
    apps: { limit: 2, used: 0, reserved: 0 },
    concurrentOperations: { limit: 1, used: 0, reserved: 0 },
  },
});
function show(path: string, role: Session['role']) {
  vi.stubGlobal('fetch', vi.fn(pending));
  vi.spyOn(api, 'session').mockResolvedValue(session(role));
  window.history.replaceState(null, '', path);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  );
}
afterEach(() => {
  cleanup();
  clearCredentials();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('task routes', () => {
  it.each(['owner', 'staff', 'admin'] as const)('opens the same app pages for %s', async (role) => {
    const read = vi.spyOn(api, 'app').mockImplementation(pending);
    show(`/apps/${app}/deployments/${deployment}`, role);
    await waitFor(() => expect(read).toHaveBeenCalledWith(app));
    expect(screen.queryByRole('heading', { name: 'Page not found' })).toBeNull();
  });
  it.each(['/all-apps', '/people', '/activity', '/audit'])('gates %s for owners', async (path) => {
    show(path, 'owner');
    expect(
      await screen.findByRole('heading', { name: "You don't have access to this page" }),
    ).toBeVisible();
  });
  it('gates audit for staff', async () => {
    const audit = vi.spyOn(adminApi, 'audit');
    show('/audit', 'staff');
    expect(
      await screen.findByRole('heading', { name: "You don't have access to this page" }),
    ).toBeVisible();
    expect(audit).not.toHaveBeenCalled();
  });
  it.each([
    '/staff/owners',
    '/staff/apps/x',
    '/staff/operations',
    '/admin/apps',
    '/admin/apps/x',
    '/admin/accounts',
    '/admin/audit',
  ])('has no retired route %s', async (path) => {
    show(path, 'admin');
    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeVisible();
    expect(window.location.pathname).toBe(path);
  });
});
