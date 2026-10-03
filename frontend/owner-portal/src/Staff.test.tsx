import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { api, clearCredentials, type Session } from './api';
import { StaffPages } from './pages/Staff';
import { staffApi } from './staffApi';

const userId = '11111111-1111-4111-8111-111111111111';
const ownerId = '22222222-2222-4222-8222-222222222222';
const owner = {
  ownerId,
  username: 'student',
  displayName: '<script>Student</script>',
  portalEnabled: true,
  role: 'owner' as const,
};
const ownerName = { ownerUsername: 'student', ownerDisplayName: 'Alice Student' };
const session = (role: Session['role']): Session => ({
  role,
  stepUpExpiresAt: null,
  csrfToken: 'staff-csrf',
  expiresAt: new Date(Date.now() + 3600000).toISOString(),
  user: { id: userId, username: 'instructor', displayName: 'Instructor' },
  quota: {
    apps: { limit: 2, used: 0, reserved: 0 },
    concurrentOperations: { limit: 1, used: 0, reserved: 0 },
  },
});
const clients: QueryClient[] = [];
function show(path: string, node = <App />) {
  window.history.replaceState(null, '', path);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  clients.push(client);
  render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
  return client;
}
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  clearCredentials();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('staff navigation and sign-in', () => {
  it('shows a read-only directory, escapes profile text and never mounts owner mutation hooks', async () => {
    vi.spyOn(api, 'session').mockResolvedValue(session('staff'));
    const owners = vi
      .spyOn(staffApi, 'owners')
      .mockResolvedValue({ items: [owner], nextCursor: null, truncated: false });
    const apps = vi.spyOn(api, 'apps');
    const create = vi.spyOn(api, 'create');
    const resume = vi.spyOn(api, 'resume');
    const client = show('/staff/owners');
    expect(await screen.findByText(owner.displayName)).toBeVisible();
    expect(screen.getByRole('navigation', { name: 'Staff pages' })).toBeVisible();
    expect(screen.queryByRole('link', { name: 'Create application' })).not.toBeInTheDocument();
    expect(document.querySelector('script')).toBeNull();
    expect(screen.queryByText(/read only/i)).not.toBeInTheDocument();
    expect(apps).not.toHaveBeenCalled();
    expect(create).not.toHaveBeenCalled();
    expect(resume).not.toHaveBeenCalled();
    expect(owners).toHaveBeenCalledTimes(1);
    expect(client.getQueryData(['staff', 'staff', userId, 'owners', undefined])).toBeDefined();
  });
  it('blocks staff direct URLs for owner sessions without querying the catalog', async () => {
    vi.spyOn(api, 'session').mockResolvedValue(session('owner'));
    const owners = vi.spyOn(staffApi, 'owners');
    show('/staff/owners');
    expect(
      await screen.findByRole('heading', { name: "You don't have access to this page" }),
    ).toBeVisible();
    expect(owners).not.toHaveBeenCalled();
    expect(screen.queryByRole('navigation', { name: 'Staff pages' })).not.toBeInTheDocument();
  });
  it('clears staff cache and returns to staff credential entry when access ends', async () => {
    vi.spyOn(api, 'session').mockResolvedValue(session('staff'));
    vi.spyOn(staffApi, 'owners').mockResolvedValue({
      items: [owner],
      nextCursor: null,
      truncated: false,
    });
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ data: { csrfToken: 'anon', providerLabel: 'class account' } }),
      }),
    );
    const client = show('/staff/owners');
    await screen.findByText(owner.displayName);
    act(() => window.dispatchEvent(new Event('portal-session-ended')));
    expect(await screen.findByRole('heading', { name: /^Sign in/ })).toBeVisible();
    expect(screen.queryByText(owner.displayName)).not.toBeInTheDocument();
    expect(client.getQueryCache().findAll({ queryKey: ['staff'] })).toHaveLength(0);
  });
  it('pauses polling in a hidden tab and ends an idle staff view', async () => {
    vi.useFakeTimers();
    vi.spyOn(api, 'session').mockResolvedValue(session('staff'));
    const operations = vi
      .spyOn(staffApi, 'operations')
      .mockResolvedValue({ items: [], nextCursor: null, truncated: false });
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ data: { csrfToken: 'anon', providerLabel: 'class account' } }),
      }),
    );
    show('/staff/operations');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(operations).toHaveBeenCalledTimes(1);
    vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
    act(() => document.dispatchEvent(new Event('visibilitychange')));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60000);
    });
    expect(operations).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600000);
    });
    expect(window.location.pathname).toBe('/sign-in');
    expect(screen.queryByRole('heading', { name: 'Activity' })).not.toBeInTheDocument();
  });
});

describe('staff controller diagnostics', () => {
  it('shows a controller code in the operations view', async () => {
    vi.spyOn(api, 'session').mockResolvedValue(session('staff'));
    vi.spyOn(staffApi, 'operations').mockResolvedValue({
      items: [
        {
          intentId: userId,
          applicationId: ownerId,
          applicationSlug: 'weather-dashboard',
          ownerId,
          ...ownerName,
          kind: 'deploy',
          state: 'failed',
          stage: 'settled',
          cleanupState: 'unknown',
          createdAt: new Date().toISOString(),
          updatedAt: null,
          statusObservedAt: null,
          attention: 'failed',
          controllerErrorCode: 'INVALID_REQUEST',
          guidance: 'Check that the repository root contains package.json.',
        },
      ],
      nextCursor: null,
      truncated: false,
    });
    show('/staff/operations');
    expect(await screen.findByText('INVALID_REQUEST')).toBeVisible();
    expect(screen.getByText('Check that the repository root contains package.json.')).toBeVisible();
  });
});

const appId = '33333333-3333-4333-8333-333333333333';
const deploymentId = '44444444-4444-4444-8444-444444444444';
const commit = 'abcdef0123456789abcdef0123456789abcdef01';
const catalogApp = {
  applicationId: appId,
  ownerId,
  slug: 'weather-dashboard',
  lifecycleState: 'ready',
  savedRevision: 2,
  createdAt: new Date().toISOString(),
  repository: 'https://github.com/example/weather-dashboard',
};
const appRow = { ...catalogApp, ...ownerName };
const empty = { items: [], nextCursor: null, truncated: false };
function staffPage(path: string) {
  return show(path, <StaffPages userId={userId} />);
}

describe('staff detail pages', () => {
  it('shows an app read-only, with names instead of IDs and chained reads', async () => {
    let active = 0;
    let peak = 0;
    const track =
      <T,>(value: T) =>
      async () => {
        peak = Math.max(peak, ++active);
        await new Promise((done) => setTimeout(done, 5));
        active--;
        return value;
      };
    const app = vi.spyOn(staffApi, 'app').mockImplementation(
      track({
        ...catalogApp,
        url: 'https://weather-dashboard.apps.example.com',
        desiredRunning: true,
        activeDeploymentId: deploymentId,
        acceptedDeployment: { deploymentId, sourceCommit: commit, acceptedAt: null },
        health: { process: 'healthy', route: 'unhealthy' },
        observedAt: new Date().toISOString(),
        stale: false,
      }),
    );
    const deployments = vi.spyOn(staffApi, 'deployments').mockImplementation(
      track({
        items: [
          {
            deploymentId,
            applicationId: appId,
            status: 'succeeded',
            repositoryCommit: commit,
            configurationRevision: 2,
            cleanupState: 'confirmed',
            requestedAt: null,
            updatedAt: null,
            acceptedAt: null,
            lastHealthyAt: null,
          },
        ],
        nextCursor: null,
        truncated: false,
      }),
    );
    vi.spyOn(staffApi, 'operations').mockImplementation(track(empty));
    vi.spyOn(staffApi, 'owner').mockImplementation(
      track({
        ...owner,
        displayName: 'Alice Student',
        quota: {
          apps: { limit: 2, used: 1, reserved: 0 },
          concurrentOperations: { limit: 1, used: 0, reserved: 0 },
        },
      }),
    );
    staffPage(`/staff/apps/${appId}`);
    expect(await screen.findByRole('heading', { name: 'weather-dashboard' })).toBeVisible();
    expect(await screen.findByRole('link', { name: 'Alice Student' })).toHaveAttribute(
      'href',
      `/staff/owners/${ownerId}`,
    );
    expect(app).toHaveBeenCalledTimes(1);
    expect(deployments).toHaveBeenCalledTimes(1);
    // Two active reads per account at most; never two controller reads at once.
    expect(peak).toBe(1);
    expect(screen.getByText('Live')).toBeVisible();
    // Health badges only where it needs a look: the title and the failing check.
    expect(screen.getAllByText('Unhealthy')).toHaveLength(2);
    expect(screen.getByText('Healthy').closest('.ui-badge')).toBeNull();
    expect(screen.queryByText(appId)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Copy app ID' })).toBeVisible();
    // Read-only: refresh and copy are the only buttons.
    expect(
      screen
        .getAllByRole('button')
        .map((button) => button.textContent || button.getAttribute('aria-label')),
    ).toEqual(['Refresh', 'Copy app ID']);
  });
  it('shows activity in plain words without resume actions', async () => {
    vi.spyOn(staffApi, 'operations').mockResolvedValue({
      items: [
        {
          intentId: userId,
          applicationId: appId,
          applicationSlug: 'weather-dashboard',
          ownerId,
          ...ownerName,
          kind: 'save_configuration',
          state: 'blocked',
          stage: 'recovery',
          cleanupState: 'pending',
          createdAt: new Date().toISOString(),
          updatedAt: null,
          statusObservedAt: null,
          attention: 'awaiting_controller',
          controllerErrorCode: null,
          guidance: null,
        },
      ],
      nextCursor: null,
      truncated: false,
    });
    const owners = vi.spyOn(staffApi, 'owners');
    const apps = vi.spyOn(staffApi, 'apps');
    staffPage('/staff/operations');
    expect(await screen.findByRole('heading', { name: 'Activity' })).toBeVisible();
    expect(await screen.findByText('Save settings')).toBeVisible();
    expect(await screen.findByRole('link', { name: 'weather-dashboard' })).toBeVisible();
    expect(await screen.findByRole('link', { name: 'Alice Student' })).toBeVisible();
    // Names come with the rows: no directory reads.
    expect(owners).not.toHaveBeenCalled();
    expect(apps).not.toHaveBeenCalled();
    expect(screen.getByText('Waiting for the platform')).toBeVisible();
    expect(screen.getByText('Needs attention')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Resume' })).not.toBeInTheDocument();
    expect(screen.queryByText(/controller|intent|operation/i)).not.toBeInTheDocument();
  });
  it('badges only staff roles and disabled accounts in the owner list', async () => {
    vi.spyOn(staffApi, 'owners').mockResolvedValue({
      items: [
        owner,
        {
          ...owner,
          ownerId: appId,
          displayName: 'Taylor',
          username: 'taylor',
          role: 'staff',
        },
        {
          ...owner,
          ownerId: deploymentId,
          displayName: 'Carol',
          username: 'carol',
          portalEnabled: false,
        },
      ],
      nextCursor: null,
      truncated: false,
    });
    staffPage('/staff/owners');
    expect(await screen.findByText('Taylor')).toBeVisible();
    expect(screen.getByText('Staff')).toBeVisible();
    expect(screen.getByText('Disabled')).toBeVisible();
    expect(screen.queryByText('Active')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refresh' })).toHaveClass('ui-button--ghost');
  });
  it('lists every app with its owner name from the row', async () => {
    vi.spyOn(staffApi, 'apps').mockResolvedValue({
      items: [appRow],
      nextCursor: null,
      truncated: false,
    });
    const owners = vi.spyOn(staffApi, 'owners');
    staffPage('/staff/apps');
    expect(await screen.findByRole('link', { name: 'Alice Student' })).toHaveAttribute(
      'href',
      `/staff/owners/${ownerId}`,
    );
    expect(owners).not.toHaveBeenCalled();
  });
  it('pages through owners with the server cursor', async () => {
    const next = '55555555-5555-4555-8555-555555555555';
    const owners = vi
      .spyOn(staffApi, 'owners')
      .mockResolvedValueOnce({ items: [owner], nextCursor: next, truncated: true })
      .mockResolvedValueOnce({ items: [], nextCursor: null, truncated: false });
    staffPage('/staff/owners');
    fireEvent.click(await screen.findByRole('button', { name: 'Next page' }));
    expect(await screen.findByRole('heading', { name: 'No owners yet' })).toBeVisible();
    expect(owners).toHaveBeenLastCalledWith(next, expect.anything());
    expect(screen.getByRole('button', { name: 'First page' })).toBeVisible();
  });
});

describe('staff data API', () => {
  it('attaches session CSRF to GET and refreshes it once', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: false,
        status: 403,
        json: async () => ({ error: { code: 'CSRF_REJECTED' } }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ data: session('staff') }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ data: { items: [owner], nextCursor: null, truncated: false } }),
      });
    vi.stubGlobal('fetch', fetch);
    await staffApi.owners();
    expect(fetch.mock.calls[2][1].headers['X-CSRF-Token']).toBe('staff-csrf');
    expect(fetch.mock.calls[2][1].method).toBeUndefined();
    expect(fetch.mock.calls[2][1].body).toBeUndefined();
  });
  it('rejects malformed or expanded metadata instead of displaying raw payloads', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({
          data: { items: [{ ...owner, refs: 'SECRET' }], nextCursor: null, truncated: false },
        }),
      }),
    );
    await expect(staffApi.owners()).rejects.toThrow('Invalid staff metadata');
  });
  it('rejects unknown roles and app rows without owner names', async () => {
    const reply = (data: unknown) =>
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ data }) }),
      );
    reply({ items: [{ ...owner, role: 'root' }], nextCursor: null, truncated: false });
    await expect(staffApi.owners()).rejects.toThrow('Invalid staff metadata');
    reply({ items: [catalogApp], nextCursor: null, truncated: false });
    await expect(staffApi.apps()).rejects.toThrow('Invalid staff metadata');
    reply({ items: [appRow], nextCursor: null, truncated: false });
    await expect(staffApi.apps()).resolves.toMatchObject({ items: [appRow] });
  });
  it('drops a late response after credentials and private caches were cleared', async () => {
    let resolve!: (v: unknown) => void;
    vi.stubGlobal(
      'fetch',
      vi.fn().mockReturnValue(
        new Promise((done) => {
          resolve = done;
        }),
      ),
    );
    const pending = staffApi.owners();
    clearCredentials();
    resolve({
      ok: true,
      status: 200,
      json: async () => ({ data: { items: [owner], nextCursor: null, truncated: false } }),
    });
    await expect(pending).rejects.toThrow('Sign in to continue');
  });
});

describe('admin-only account pages', () => {
  it('denies account and audit pages to owner and staff sessions without admin API calls', async () => {
    const { adminApi } = await import('./adminApi');
    const listing = vi.spyOn(adminApi, 'accounts');
    vi.spyOn(api, 'session').mockResolvedValue(session('staff'));
    show('/admin/accounts');
    expect(
      await screen.findByRole('heading', { name: "You don't have access to this page" }),
    ).toBeVisible();
    expect(listing).not.toHaveBeenCalled();
  });
  it('offers two identity methods without a role selector or staff sign-in upgrade', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          data: {
            csrfToken: 'anon',
            providerLabel: 'example university account',
            platformName: 'Example Platform',
          },
        }),
      }),
    );
    show('/sign-in');
    // The method and brand come from server configuration, never hardcoded names.
    expect(await screen.findByRole('radio', { name: 'Example university account' })).toBeChecked();
    expect(await screen.findByText('Example Platform')).toBeVisible();
    expect(screen.getByRole('radio', { name: 'Local account' })).toBeVisible();
    expect(screen.queryByRole('link', { name: 'Staff sign-in' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('radio', { name: 'Local account' }));
    expect(screen.getByLabelText('Authentication code')).toBeVisible();
    expect(screen.queryByRole('combobox', { name: /role/i })).not.toBeInTheDocument();
  });
});
