import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { appManagementApi } from './appManagementApi';
import { ApiError, api, clearCredentials, type AppRecord, type Session } from './api';
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
function mockApps(items: AppRecord[], used = items.length, limit: number | null = 2) {
  vi.spyOn(api, 'apps').mockResolvedValue({
    items,
    nextCursor: null,
    truncated: false,
    quota: {
      apps: { used, reserved: 0, limit },
      concurrentOperations: { used: 0, reserved: 0, limit: limit === null ? null : 1 },
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
    expect(await screen.findByRole('heading', { name: 'My apps', level: 1 })).toBeVisible();
    expect(screen.getByRole('link', { name: 'Example Platform home' })).toBeVisible();
    // The apps section is titled by the brand alone; other sections lead with their name.
    await waitFor(() => expect(document.title).toBe('My apps · Example Platform'));
    expect(document.body.textContent).not.toMatch(/Owner portal|My applications/);
    expect(pageTitle('/people')).toBe('People');
    expect(pageTitle('/people', 'Example Platform')).toBe('People · Example Platform');
    expect(pageTitle('/apps/abc/configuration', 'Example Platform')).toBe(
      'My apps · Example Platform',
    );
  });
  it('shows section links by role and signs out from the account menu', async () => {
    vi.spyOn(api, 'session').mockResolvedValue(session('admin'));
    mockApps([app]);
    const logout = vi.spyOn(api, 'logout').mockResolvedValue(undefined);
    show('/apps');
    const nav = (await screen.findAllByRole('navigation', { name: 'Main' }))[0];
    expect(within(nav).getByRole('link', { name: 'My apps' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    expect(within(nav).getByRole('link', { name: 'People' })).toBeVisible();
    expect(within(nav).getByRole('link', { name: 'Audit log' })).toBeVisible();
    expect(within(nav).getByRole('link', { name: 'Platform settings' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Account: Alice Student' }));
    expect(screen.getByText('Admin', { selector: '.ui-badge' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    await waitFor(() => expect(logout).toHaveBeenCalledOnce());
  });
  it('gives staff task destinations and hides the audit log', async () => {
    vi.spyOn(api, 'session').mockResolvedValue(session('staff'));
    const list = vi.spyOn(appManagementApi, 'list').mockReturnValue(new Promise(() => {}));
    show('/all-apps');
    const nav = (await screen.findAllByRole('navigation', { name: 'Main' }))[0];
    expect(within(nav).getByRole('link', { name: 'People' })).toBeVisible();
    // Each task has one destination.
    expect(within(nav).getByRole('link', { name: 'All apps' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    expect(within(nav).queryByRole('link', { name: 'Audit log' })).toBeNull();
    expect(within(nav).queryByRole('link', { name: 'Platform settings' })).toBeNull();
    expect(screen.queryByRole('navigation', { name: 'Admin pages' })).toBeNull();
    await waitFor(() => expect(list).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: 'Account: Alice Student' }));
    expect(screen.getByText('Staff', { selector: '.ui-badge' })).toBeVisible();
  });
  it('hides staff and admin links from owners', async () => {
    vi.spyOn(api, 'session').mockResolvedValue(session('owner'));
    mockApps([app]);
    show('/apps');
    await screen.findByRole('heading', { name: 'My apps', level: 1 });
    expect(screen.queryByRole('link', { name: 'People' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Audit log' })).toBeNull();
  });
});

describe('app list', () => {
  it('lists apps with status, URL and deployed commit', async () => {
    vi.spyOn(api, 'session').mockResolvedValue(session('owner'));
    mockApps([app]);
    show('/apps');
    const table = await screen.findByRole('table', { name: 'My apps' });
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
    expect(screen.getByText('1 of 2')).toHaveTextContent('1 of 2 apps used');
    expect(screen.getByRole('link', { name: 'Create app' })).toHaveAttribute('href', '/apps/new');
  });
  it('opens an app from anywhere on its row', async () => {
    vi.spyOn(api, 'session').mockResolvedValue(session('owner'));
    mockApps([app]);
    vi.spyOn(api, 'app').mockReturnValue(new Promise(() => {}));
    show('/apps');
    fireEvent.click(await screen.findByText('abcdef012'));
    await waitFor(() => expect(window.location.pathname).toBe('/apps/app-1'));
  });
  it('badges only activity that is not a success', async () => {
    vi.spyOn(api, 'session').mockResolvedValue(session('owner'));
    mockApps([app]);
    const base = {
      appId: 'app-1',
      appSlug: 'student-project',
      commit: null,
      createdAt: new Date().toISOString(),
      kind: 'deploy',
      operationId: null,
      operation: null,
      safeError: null,
    };
    vi.mocked(api.intents).mockResolvedValue({
      items: [
        { ...base, intentId: 'ok', state: 'succeeded' },
        { ...base, intentId: 'bad', state: 'failed', safeError: 'The build failed.' },
      ],
      nextCursor: null,
      truncated: false,
    });
    show('/apps');
    const feed = await screen.findByRole('list', { name: 'Recent activity' });
    await within(feed).findByText('Failed');
    expect(within(feed).getByText('Succeeded')).toHaveClass('ui-sr-only');
    expect(within(feed).getAllByText(/Succeeded|Failed/)).toHaveLength(2);
    expect(within(feed).getByText('The build failed.')).toBeVisible();
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
    expect(await screen.findByText(/reached your limit of 2 apps/)).toBeVisible();
    expect(screen.queryByRole('link', { name: 'Create app' })).toBeNull();
  });
  it('shows admins no quota and never a full limit', async () => {
    vi.spyOn(api, 'session').mockResolvedValue(session('admin'));
    mockApps([app, { ...app, applicationId: 'app-2', slug: 'second' }], 5, null);
    show('/apps');
    expect(await screen.findByRole('link', { name: 'Create app' })).toBeVisible();
    expect(screen.queryByText(/of 2|of null|apps used/)).toBeNull();
    expect(screen.queryByText(/reached your limit/)).toBeNull();
  });
  it('keeps the page header and offers Retry when apps fail to load', async () => {
    vi.spyOn(api, 'session').mockResolvedValue(session('owner'));
    mockApps([app]);
    vi.mocked(api.apps).mockRejectedValueOnce(
      new ApiError(503, 'UNAVAILABLE', 'The service is unavailable.'),
    );
    show('/apps');
    expect(await screen.findByRole('heading', { name: 'My apps', level: 1 })).toBeVisible();
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent("Couldn't load your apps. Try again in a minute.");
    // Announced, not focused: no focus ring appears on load.
    expect(alert).not.toHaveFocus();
    fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('table', { name: 'My apps' })).toBeVisible();
  });
});
