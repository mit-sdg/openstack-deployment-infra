import type { ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '@openstack-platform/ui';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { App } from './App';
import {
  api,
  ApiError,
  request,
  record,
  configurationGuidance,
  type Session,
  type Settings,
} from './api';
import { adminApi } from './adminApi';
import { adminAppsApi, type ManagedApp } from './adminAppsApi';
import { AccountsPage } from './pages/Accounts';
import { AdminAppsPages } from './pages/AdminApps';
import { EnvironmentSection } from './components/EnvironmentSection';
const id = '00000000-0000-4000-8000-000000000081';
const settings: Settings = {
  revision: 7,
  repository: 'https://github.com/example/class-app',
  branch: 'main',
  configurationSha256: null,
  configuration: {
    schemaVersion: 1,
    build: { runtime: 'node', packages: ['.'], buildScript: null, startScript: 'start' },
    runtime: { port: 3000, healthPath: '/health' },
    storageBindings: [],
  },
};
const detail: ManagedApp = {
  applicationId: id,
  slug: 'class-fixture',
  ownerId: id,
  ownerUsername: 'admin',
  ownerDisplayName: 'Admin',
  identityProvider: true,
  requiresMaintenance: true,
  savedRevision: 7,
  lifecycleState: 'ready',
  desiredRunning: true,
  activeDeploymentId: id,
  url: 'https://example.test',
  acceptedDeployment: null,
  health: null,
  stale: false,
  observedAt: null,
  sizing: { workerFlavor: 'worker-large', cpuMHz: 4000, memoryMiB: 8192 },
};
let session: Session | undefined;
function mockSession(stepUpExpiresAt: string | null) {
  session = {
    role: 'admin',
    csrfToken: 'csrf',
    user: { id, username: 'admin', displayName: 'Admin' },
    expiresAt: new Date(Date.now() + 3600000).toISOString(),
    quota: {
      apps: { used: 0, reserved: 0, limit: 2 },
      concurrentOperations: { used: 0, reserved: 0, limit: 1 },
    },
    stepUpExpiresAt,
  } as Session;
  vi.spyOn(api, 'session').mockResolvedValue(session);
}
function show(node: ReactNode, path = '/admin/apps') {
  window.history.replaceState(null, '', path);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  // The shell keeps the session query alive; mirror that for pages rendered alone.
  client.setQueryDefaults(['session'], { gcTime: Infinity });
  if (session) client.setQueryData(['session'], session);
  session = undefined;
  render(
    <QueryClientProvider client={client}>
      <ToastProvider>{node}</ToastProvider>
    </QueryClientProvider>,
  );
  return client;
}
describe('admin application management', () => {
  it.each(['owner', 'staff'] as const)(
    'denies admin pages for %s without fetching any managed app',
    async (role) => {
      vi.spyOn(api, 'session').mockResolvedValue({
        role,
        csrfToken: 'csrf',
        user: { id, username: 'student', displayName: 'Student' },
        expiresAt: new Date(Date.now() + 3600000).toISOString(),
        quota: {
          apps: { used: 0, reserved: 0, limit: 2 },
          concurrentOperations: { used: 0, reserved: 0, limit: 1 },
        },
        stepUpExpiresAt: null,
      } as Session);
      const list = vi.spyOn(adminAppsApi, 'list');
      show(<App />);
      expect(
        await screen.findByRole('heading', { name: "You don't have access to this page" }),
      ).toBeVisible();
      expect(list).not.toHaveBeenCalled();
      expect(screen.queryByRole('link', { name: 'Admin' })).not.toBeInTheDocument();
    },
  );
  it('queues parallel metadata panels within the two-active-read bound', async () => {
    const completions: (() => void)[] = [];
    let active = 0,
      maximum = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(() => {
        active++;
        maximum = Math.max(maximum, active);
        return new Promise<Response>((resolve) =>
          completions.push(() => {
            active--;
            resolve(new Response(JSON.stringify({ data: {} }), { status: 200 }));
          }),
        );
      }),
    );
    try {
      const calls = Array.from({ length: 4 }, (_, n) => request('/admin-apps/' + n, record));
      await waitFor(() => expect(completions).toHaveLength(2));
      completions[0]();
      completions[1]();
      await waitFor(() => expect(completions).toHaveLength(4));
      completions[2]();
      completions[3]();
      await Promise.all(calls);
      expect(maximum).toBe(2);
    } finally {
      vi.unstubAllGlobals();
    }
  });
  it('requires maintenance and class-provider confirmation in the deploy dialog', async () => {
    vi.spyOn(adminAppsApi, 'detail').mockResolvedValue(detail);
    const service = {
      ...api,
      settings: vi.fn().mockResolvedValue(settings),
      environment: vi
        .fn()
        .mockResolvedValue({ revision: 0, items: [], intents: [], updatedAt: null }),
      storage: vi.fn().mockResolvedValue({ items: [], intents: [] }),
    };
    vi.spyOn(adminAppsApi, 'resources').mockReturnValue(service);
    show(<AdminAppsPages />, `/admin/apps/${id}`);
    fireEvent.click(await screen.findByRole('button', { name: 'Deploy' }));
    expect(screen.getByText(configurationGuidance.scripts)).toBeVisible();
    expect(screen.getByText(configurationGuidance.root, { exact: false })).toBeVisible();
    expect(screen.getByText(configurationGuidance.health)).toBeVisible();
    expect(screen.getByText(configurationGuidance.postgres, { exact: false })).toBeVisible();

    const dialog = screen.getByRole('dialog', { name: 'Deploy class-fixture' });
    expect(dialog).toHaveTextContent('this app keeps a fixed IP address');
    const confirm = within(dialog).getByRole('button', { name: 'Deploy' });
    expect(confirm).toBeDisabled();
    fireEvent.click(within(dialog).getByLabelText('Allow a brief outage', { exact: false }));
    expect(confirm).toBeDisabled();
    expect(dialog).toHaveTextContent('This app provides sign-in for the portal');
    fireEvent.click(within(dialog).getByLabelText('Deploy the sign-in app', { exact: false }));
    expect(confirm).toBeEnabled();
    expect(dialog).toHaveTextContent('Leave empty to keep the current size');
  });
  it('asks for the password and code when a sensitive action needs them, then continues', async () => {
    mockSession(null);
    const reauthenticate = vi
      .spyOn(adminApi, 'reauthenticate')
      .mockResolvedValue({ stepUpExpiresAt: new Date(Date.now() + 300000).toISOString() });
    const create = vi
      .spyOn(adminApi, 'create')
      .mockResolvedValue({ userId: id, setupUrl: 'https://portal.test/setup#TOKEN' });
    vi.spyOn(adminApi, 'accounts').mockResolvedValue({
      items: [],
      nextCursor: null,
      truncated: false,
    });
    show(<AccountsPage />, '/admin/accounts');
    fireEvent.click(await screen.findByRole('button', { name: 'Create account' }));
    const dialog = screen.getByRole('dialog', { name: 'Create account' });
    fireEvent.change(within(dialog).getByLabelText('Username'), { target: { value: 'newstaff' } });
    fireEvent.change(within(dialog).getByLabelText('Role'), { target: { value: 'staff' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create account' }));
    const stepUp = await screen.findByRole('dialog', { name: 'Confirm it’s you' });
    expect(create).not.toHaveBeenCalled();
    fireEvent.change(within(stepUp).getByLabelText('Password'), { target: { value: 'pw' } });
    fireEvent.change(within(stepUp).getByLabelText('Authentication code'), {
      target: { value: '123456' },
    });
    fireEvent.click(within(stepUp).getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(create).toHaveBeenCalledWith('newstaff', 'newstaff', 'staff'));
    expect(reauthenticate).toHaveBeenCalledWith('pw', '123456');
    expect(await screen.findByLabelText('Setup link')).toHaveValue(
      'https://portal.test/setup#TOKEN',
    );
  });
  it('retries after the server asks for step-up, and cancelling shows no error', async () => {
    mockSession(new Date(Date.now() + 300000).toISOString());
    vi.spyOn(adminApi, 'accounts').mockResolvedValue({
      items: [],
      nextCursor: null,
      truncated: false,
    });
    const create = vi
      .spyOn(adminApi, 'create')
      .mockRejectedValue(new ApiError(403, 'STEP_UP_REQUIRED', 'Re-enter your password.'));
    show(<AccountsPage />, '/admin/accounts');
    fireEvent.click(await screen.findByRole('button', { name: 'Create account' }));
    const dialog = screen.getByRole('dialog', { name: 'Create account' });
    fireEvent.change(within(dialog).getByLabelText('Username'), { target: { value: 'x' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create account' }));
    const stepUp = await screen.findByRole('dialog', { name: 'Confirm it’s you' });
    expect(create).toHaveBeenCalledOnce();
    fireEvent.click(within(stepUp).getByRole('button', { name: 'Cancel' }));
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Confirm it’s you' })).not.toBeInTheDocument(),
    );
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
  });
  it('requires explicit consent before adopting the app that provides sign-in', async () => {
    mockSession(new Date(Date.now() + 300000).toISOString());
    vi.spyOn(adminAppsApi, 'list').mockResolvedValue({
      items: [],
      nextCursor: null,
      truncated: false,
    });
    vi.spyOn(adminApi, 'accounts').mockResolvedValue({
      items: [],
      nextCursor: null,
      truncated: false,
    });
    const adopt = vi
      .spyOn(adminAppsApi, 'adopt')
      .mockRejectedValueOnce(
        new ApiError(409, 'IDENTITY_CONFIRMATION_REQUIRED', 'Portal sign-in depends on this app'),
      )
      .mockResolvedValueOnce({ applicationId: id });
    show(<AdminAppsPages />, '/admin/apps');
    fireEvent.click(await screen.findByRole('button', { name: 'Adopt app' }));
    const dialog = screen.getByRole('dialog', { name: 'Adopt app' });
    fireEvent.change(within(dialog).getByLabelText('App ID'), { target: { value: id } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Adopt app' }));
    await waitFor(() => expect(adopt).toHaveBeenCalledTimes(1));
    expect(adopt.mock.calls[0][3]).toBe(false);
    const consent = await within(dialog).findByLabelText('Adopt the sign-in app');
    expect(within(dialog).getByRole('button', { name: 'Adopt app' })).toBeDisabled();
    fireEvent.click(consent);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Adopt app' }));
    await waitFor(() => expect(adopt).toHaveBeenCalledTimes(2));
    expect(adopt.mock.calls[1][3]).toBe(true);
    expect(adopt.mock.calls[1][0]).toBe(id);
  });
  it('deletes storage only after the typed confirmation matches', async () => {
    mockSession(new Date(Date.now() + 300000).toISOString());
    vi.spyOn(adminAppsApi, 'detail').mockResolvedValue({ ...detail, identityProvider: false });
    const resource = {
      resourceId: '00000000-0000-4000-8000-0000000000aa',
      type: 'postgres' as const,
      label: 'main',
      status: 'ready',
      createdAt: new Date().toISOString(),
      verifiedAt: null,
      defaultBindings: {},
    };
    vi.spyOn(adminAppsApi, 'resources').mockReturnValue({
      ...api,
      settings: vi.fn().mockResolvedValue(settings),
      environment: vi
        .fn()
        .mockResolvedValue({ revision: 0, items: [], intents: [], updatedAt: null }),
      storage: vi.fn().mockResolvedValue({ items: [resource], intents: [] }),
    });
    const remove = vi
      .spyOn(adminAppsApi, 'deleteStorage')
      .mockResolvedValue({ intentId: id, state: 'accepted' } as never);
    vi.spyOn(api, 'intent').mockResolvedValue({ intentId: id, state: 'accepted' } as never);
    show(<AdminAppsPages />, `/admin/apps/${id}`);
    const open = await screen.findByRole('button', { name: 'Delete' });
    await waitFor(() => expect(open).toBeEnabled());
    fireEvent.click(open);
    const dialog = screen.getByRole('dialog', { name: 'Delete a database or storage' });
    fireEvent.change(within(dialog).getByLabelText('Database or storage'), {
      target: { value: resource.resourceId },
    });
    const confirm = within(dialog).getByRole('button', { name: 'Delete permanently' });
    fireEvent.change(within(dialog).getByLabelText('Type “class-fixture postgres” to confirm'), {
      target: { value: 'class-fixture' },
    });
    expect(confirm).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText('Type “class-fixture postgres” to confirm'), {
      target: { value: 'class-fixture postgres' },
    });
    fireEvent.click(confirm);
    await waitFor(() =>
      expect(remove).toHaveBeenCalledWith(
        id,
        resource.resourceId,
        'class-fixture postgres',
        true,
        expect.any(String),
      ),
    );
  });
  it('reuses the write-only field without storing admin values in mutation or query caches', async () => {
    const service = {
      ...api,
      environment: vi
        .fn()
        .mockResolvedValue({ revision: 0, items: [], intents: [], updatedAt: null }),
      setEnvironment: vi
        .fn()
        .mockResolvedValue({ intentId: id, kind: 'env_set', state: 'accepted', appId: id }),
    };
    vi.spyOn(api, 'intent').mockResolvedValue({
      intentId: id,
      kind: 'env_set',
      state: 'succeeded',
      appId: id,
    } as never);
    const client = show(<EnvironmentSection id={id} bindings={[]} service={service} />);
    await screen.findByLabelText('Variable name');
    fireEvent.change(screen.getByLabelText('Variable name'), { target: { value: 'TOKEN' } });
    fireEvent.change(screen.getByLabelText('New value'), {
      target: { value: 'ADMIN_SECRET_SENTINEL' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save variable' }));
    await waitFor(() => expect(service.setEnvironment).toHaveBeenCalledOnce());
    expect(screen.getByLabelText('New value')).toHaveValue('');
    expect(
      JSON.stringify(
        client
          .getMutationCache()
          .getAll()
          .map((m) => m.state.variables),
      ),
    ).not.toContain('ADMIN_SECRET_SENTINEL');
    expect(
      JSON.stringify(
        client
          .getQueryCache()
          .getAll()
          .map((q) => q.state.data),
      ),
    ).not.toContain('ADMIN_SECRET_SENTINEL');
  });
});
