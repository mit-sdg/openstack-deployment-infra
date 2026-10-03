import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { api, clearCredentials, type Session } from './api';
import { staffApi } from './staffApi';

const userId = '11111111-1111-4111-8111-111111111111';
const ownerId = '22222222-2222-4222-8222-222222222222';
const owner = {
  ownerId,
  username: 'student',
  displayName: '<script>Student</script>',
  portalEnabled: true,
};
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
    expect(screen.queryByRole('heading', { name: 'Operations' })).not.toBeInTheDocument();
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
          ownerId,
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
