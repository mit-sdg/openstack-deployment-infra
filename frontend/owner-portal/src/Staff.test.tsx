import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { api, clearCredentials, type Session } from './api';
import { SignIn } from './pages/SignIn';
import { staffApi } from './staffApi';

const userId = '11111111-1111-4111-8111-111111111111';
const ownerId = '22222222-2222-4222-8222-222222222222';
const owner = {
  ownerId,
  username: 'student',
  displayName: '<script>Student</script>',
  portalEnabled: true,
};
const session = (kind: Session['kind']): Session => ({
  kind,
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
    vi.spyOn(api, 'session').mockResolvedValue(session('staff_read'));
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
    expect(client.getQueryData(['staff', 'staff_read', userId, 'owners', undefined])).toBeDefined();
  });
  it('blocks owner write-page navigation from staff without owner API calls', async () => {
    vi.spyOn(api, 'session').mockResolvedValue(session('staff_read'));
    const settings = vi.spyOn(api, 'settings');
    const create = vi.spyOn(api, 'create');
    show('/apps/new');
    expect(await screen.findByRole('heading', { name: 'Read-only staff session' })).toBeVisible();
    expect(settings).not.toHaveBeenCalled();
    expect(create).not.toHaveBeenCalled();
  });
  it('blocks staff direct URLs for owner sessions without querying the catalog', async () => {
    vi.spyOn(api, 'session').mockResolvedValue(session('owner'));
    const owners = vi.spyOn(staffApi, 'owners');
    show('/staff/owners');
    expect(await screen.findByRole('heading', { name: 'Staff access unavailable' })).toBeVisible();
    expect(owners).not.toHaveBeenCalled();
    expect(screen.queryByRole('navigation', { name: 'Staff pages' })).not.toBeInTheDocument();
  });
  it('uses explicit staff mode and only shows its availability error after submitting credentials', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ data: { csrfToken: 'anonymous', providerLabel: 'class account' } }),
      })
      .mockResolvedValueOnce({
        ok: false,
        json: async () => ({ error: { code: 'STAFF_UNAVAILABLE' } }),
      });
    vi.stubGlobal('fetch', fetch);
    show('/signin?mode=staff', <SignIn />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sign in' })).toBeEnabled());
    expect(
      screen.queryByText('Staff sign-in is not available for this account.'),
    ).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'instructor' } });
    fireEvent.change(screen.getByLabelText('Password', { exact: true }), {
      target: { value: 'local-password' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('not available for this account');
    expect(JSON.parse(fetch.mock.calls[1][1].body).mode).toBe('staff');
    expect(screen.getByLabelText('Password', { exact: true })).toHaveValue('');
    expect(screen.getByRole('link', { name: 'Owner sign-in' })).toHaveAttribute('href', '/sign-in');
  });
  it('clears staff cache and returns to staff credential entry when access ends', async () => {
    vi.spyOn(api, 'session').mockResolvedValue(session('staff_read'));
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
    expect(
      await screen.findByRole('heading', { name: 'Staff sign-in with your class account' }),
    ).toBeVisible();
    expect(screen.queryByText(owner.displayName)).not.toBeInTheDocument();
    expect(client.getQueryCache().findAll({ queryKey: ['staff'] })).toHaveLength(0);
  });
  it('pauses polling in a hidden tab and ends an idle staff view', async () => {
    vi.useFakeTimers();
    vi.spyOn(api, 'session').mockResolvedValue(session('staff_read'));
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
    expect(window.location.pathname).toBe('/signin');
    expect(screen.queryByRole('heading', { name: 'Operations' })).not.toBeInTheDocument();
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
        json: async () => ({ data: session('staff_read') }),
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
