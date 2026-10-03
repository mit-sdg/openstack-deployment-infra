import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { api, clearCredentials, type AppRecord, type Session } from './api';
import { pageTitle } from './shell/PortalShell';

const session = (role: Session['role']): Session => ({
  role,
  stepUpExpiresAt: null,
  csrfToken: 'csrf',
  expiresAt: new Date(Date.now() + 3600000).toISOString(),
  user: { id: 'user', username: 'alice', displayName: 'Alice Student' },
  quota: {
    apps: { limit: 2, used: 0, reserved: 0 },
    concurrentOperations: { limit: 1, used: 0, reserved: 0 },
  },
  platformName: 'Example Platform',
});
const app: AppRecord = {
  applicationId: 'app-1',
  slug: 'student-project',
  url: 'https://student-project.apps.example.com',
  savedRevision: 2,
  lifecycleState: 'ready',
  desiredRunning: true,
  activeDeploymentId: 'deployment',
  acceptedDeployment: {
    deploymentId: 'deployment',
    sourceCommit: 'abcdef0123456789abcdef0123456789abcdef01',
    acceptedAt: new Date().toISOString(),
  },
  health: { allocationHealthy: true, routeHealthy: true, schedulerState: 'running' },
  stale: false,
  observedAt: null,
};
function mockApps(items: AppRecord[], used = items.length) {
  vi.spyOn(api, 'apps').mockResolvedValue({
    items,
    nextCursor: null,
    truncated: false,
    quota: {
      apps: { used, reserved: 0, limit: 2 },
      concurrentOperations: { used: 0, reserved: 0, limit: 1 },
    },
  });
  vi.spyOn(api, 'intents').mockResolvedValue({ items: [], nextCursor: null, truncated: false });
}
function show(path: string) {
  window.history.replaceState(null, '', path);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  );
  return client;
}
afterEach(() => {
  cleanup();
  clearCredentials();
  vi.restoreAllMocks();
});

describe('portal shell', () => {
  it('brands the shell and title from configuration, never a hardcoded name', async () => {
    vi.spyOn(api, 'session').mockResolvedValue(session('owner'));
    mockApps([app]);
    show('/apps');
    expect(await screen.findByRole('heading', { name: 'Apps', level: 1 })).toBeVisible();
    expect(screen.getByRole('link', { name: 'Example Platform home' })).toBeVisible();
    await waitFor(() => expect(document.title).toBe('Apps · Example Platform'));
    expect(document.body.textContent).not.toMatch(/Owner portal|My applications/);
    expect(pageTitle('/staff/owners')).toBe('Staff');
  });
  it('shows section links by role and signs out from the account menu', async () => {
    vi.spyOn(api, 'session').mockResolvedValue(session('admin'));
    mockApps([app]);
    const logout = vi.spyOn(api, 'logout').mockResolvedValue(undefined);
    show('/apps');
    const nav = (await screen.findAllByRole('navigation', { name: 'Main' }))[0];
    expect(within(nav).getByRole('link', { name: 'Apps' })).toHaveAttribute('aria-current', 'page');
    expect(within(nav).getByRole('link', { name: 'Staff' })).toBeVisible();
    expect(within(nav).getByRole('link', { name: 'Admin' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Account: Alice Student' }));
    expect(screen.getByText('Admin', { selector: '.ui-badge' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    await waitFor(() => expect(logout).toHaveBeenCalledOnce());
  });
  it('hides staff and admin links from owners', async () => {
    vi.spyOn(api, 'session').mockResolvedValue(session('owner'));
    mockApps([app]);
    show('/apps');
    await screen.findByRole('heading', { name: 'Apps', level: 1 });
    expect(screen.queryByRole('link', { name: 'Staff' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Admin' })).toBeNull();
  });
});

describe('app list', () => {
  it('lists apps with status, URL and deployed commit', async () => {
    vi.spyOn(api, 'session').mockResolvedValue(session('owner'));
    mockApps([app]);
    show('/apps');
    const table = await screen.findByRole('table', { name: 'Apps' });
    const row = within(table).getAllByRole('row')[1];
    expect(within(row).getByRole('link', { name: 'student-project' })).toHaveAttribute(
      'href',
      '/apps/app-1',
    );
    expect(within(row).getByText('Healthy')).toBeVisible();
    expect(within(row).getByText('abcdef012')).toBeVisible();
    expect(within(row).getByRole('link', { name: /student-project\.apps/ })).toHaveAttribute(
      'rel',
      'noopener noreferrer',
    );
    expect(screen.getByText('1 of 2 apps')).toBeVisible();
    expect(screen.getByRole('link', { name: 'Create app' })).toHaveAttribute('href', '/apps/new');
  });
  it('offers one create action when there are no apps', async () => {
    vi.spyOn(api, 'session').mockResolvedValue(session('owner'));
    mockApps([]);
    show('/apps');
    expect(await screen.findByRole('heading', { name: 'Create your first app' })).toBeVisible();
    expect(screen.getAllByRole('link', { name: 'Create app' })).toHaveLength(1);
    expect(screen.queryByRole('table')).toBeNull();
  });
  it('explains a full quota instead of offering create', async () => {
    vi.spyOn(api, 'session').mockResolvedValue(session('owner'));
    mockApps([app, { ...app, applicationId: 'app-2', slug: 'second' }]);
    show('/apps');
    expect(await screen.findByText(/used all 2 of your apps/)).toBeVisible();
    expect(screen.queryByRole('link', { name: 'Create app' })).toBeNull();
  });
});
