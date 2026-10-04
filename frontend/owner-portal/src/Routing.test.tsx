import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { adminAppsApi } from './adminAppsApi';
import { api, clearCredentials, type Session } from './api';
import { staffApi } from './staffApi';

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

describe('nested routes', () => {
  it.each([
    ['owner detail', `/staff/owners/${owner}`, 'owner', [owner]],
    ['app detail', `/staff/apps/${app}`, 'app', [app]],
    ['deployment history', `/staff/apps/${app}/deployments`, 'deployments', [app]],
    [
      'deployment detail',
      `/staff/apps/${app}/deployments/${deployment}`,
      'deployment',
      [app, deployment],
    ],
  ] as const)('opens the staff %s page', async (_name, path, method, ids) => {
    const read = vi.spyOn(staffApi, method).mockImplementation(pending);
    show(path, 'staff');
    await waitFor(() => expect(read).toHaveBeenCalled());
    expect(read.mock.calls[0].slice(0, ids.length)).toEqual(ids);
    expect(screen.queryByRole('heading', { name: 'Page not found' })).toBeNull();
  });
  it('opens a managed app for admins', async () => {
    const detail = vi.spyOn(adminAppsApi, 'detail').mockImplementation(pending);
    show(`/admin/apps/${app}`, 'admin');
    await waitFor(() => expect(detail).toHaveBeenCalledWith(app));
    expect(screen.queryByRole('heading', { name: 'Page not found' })).toBeNull();
  });
  it('keeps nested admin pages closed to staff', async () => {
    const detail = vi.spyOn(adminAppsApi, 'detail').mockImplementation(pending);
    show(`/admin/apps/${app}`, 'staff');
    expect(
      await screen.findByRole('heading', { name: "You don't have access to this page" }),
    ).toBeVisible();
    expect(detail).not.toHaveBeenCalled();
  });
});
