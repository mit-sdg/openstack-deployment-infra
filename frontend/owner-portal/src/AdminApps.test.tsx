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
function mockSession(stepUpExpiresAt: string | null, role: Session['role'] = 'admin') {
  session = {
    role,
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
/** Unmocked requests fail at once: none leaves the test or holds an admin read slot. */
function offline() {
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response(JSON.stringify({ error: { code: 'NOT_FOUND', summary: 'Not found.' } }), {
          status: 404,
        }),
    ),
  );
}
/** A managed app's resources, all read from mocks. */
function mockResources(items: unknown[] = []) {
  const service = {
    ...api,
    settings: vi.fn().mockResolvedValue(settings),
    environment: vi
      .fn()
      .mockResolvedValue({ revision: 0, items: [], intents: [], updatedAt: null }),
    storage: vi.fn().mockResolvedValue({ items, intents: [] }),
  };
  vi.spyOn(adminAppsApi, 'resources').mockReturnValue(service);
  return service;
}
describe('admin application management', () => {
  it.each(['staff', 'admin'] as const)(
    'shows and resumes another actor’s blocked deploy for %s',
    async (role) => {
      offline();
      mockSession(null, role);
      vi.spyOn(adminAppsApi, 'detail').mockResolvedValue({ ...detail, identityProvider: false });
      mockResources();
      const blocked = {
        intentId: 'blocked-deploy',
        appId: id,
        appSlug: 'class-fixture',
        kind: 'deploy',
        state: 'blocked',
        commit: 'a'.repeat(40),
        createdAt: new Date().toISOString(),
        operationId: 'deployment',
        operation: {
          status: 'recovery_required',
          phase: 'startup_interrupted',
          cleanupState: 'pending',
        },
        safeError: 'This deploy hasn’t finished. Resume it from Activity.',
        actor: { displayName: 'Alice Student', you: false },
        canResume: true,
      };
      const attention = vi.spyOn(adminAppsApi, 'attention').mockResolvedValue([blocked]);
      const ownerAttention = vi.spyOn(api, 'attention');
      const resume = vi.spyOn(api, 'resume').mockImplementation(async () => {
        attention.mockResolvedValue([]);
        return { ...blocked, state: 'accepted' };
      });
      show(<AdminAppsPages />, `/admin/apps/${id}`);
      expect(await screen.findByText('Alice Student', { exact: true })).toBeVisible();
      expect(screen.getByText('Needs attention', { exact: true })).toBeVisible();
      expect(screen.getByText(blocked.safeError)).toBeVisible();
      // Managed views must not link staff into the owner-only deployment route.
      expect(screen.queryByRole('link', { name: 'Deployment' })).toBeNull();
      fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
      await waitFor(() => expect(resume).toHaveBeenCalledWith('blocked-deploy'));
      await waitFor(() => expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull());
      expect(ownerAttention).not.toHaveBeenCalled();
    },
  );
  it('shows a blocked change started here once in Activity', async () => {
    offline();
    mockSession(null, 'staff');
    vi.spyOn(adminAppsApi, 'detail').mockResolvedValue({ ...detail, identityProvider: false });
    mockResources();
    const blocked = {
      intentId: 'blocked-stop',
      appId: id,
      appSlug: 'class-fixture',
      kind: 'app_disable',
      state: 'blocked',
      commit: null,
      createdAt: new Date().toISOString(),
      operationId: 'stop',
      operation: null,
      safeError: 'This change hasn’t finished. Resume it from Activity.',
      actor: { displayName: 'Admin', you: true },
      canResume: true,
    };
    const attention = vi.spyOn(adminAppsApi, 'attention').mockResolvedValue([]);
    vi.spyOn(api, 'intent').mockResolvedValue(blocked);
    vi.spyOn(adminAppsApi, 'state').mockImplementation(async () => {
      attention.mockResolvedValue([blocked]);
      return blocked;
    });
    show(<AdminAppsPages />, `/admin/apps/${id}`);
    fireEvent.click(await screen.findByRole('button', { name: 'Stop app' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Stop app' }));
    expect(await screen.findByRole('button', { name: 'Resume' })).toBeVisible();
    expect(screen.getAllByRole('button', { name: 'Resume' })).toHaveLength(1);
    expect(screen.queryByText('Latest change', { exact: true })).toBeNull();
  });
  it('shows the recovery instruction when a managed environment edit is refused', async () => {
    offline();
    mockSession(null, 'admin');
    vi.spyOn(adminAppsApi, 'detail').mockResolvedValue({ ...detail, identityProvider: false });
    vi.spyOn(adminAppsApi, 'attention').mockResolvedValue([]);
    const service = mockResources();
    const copy = "A previous deploy hasn't finished. Resume it from Activity.";
    service.setEnvironment = vi.fn().mockRejectedValue(new ApiError(409, 'APP_BUSY', copy));
    show(<AdminAppsPages />, `/admin/apps/${id}`);
    fireEvent.change(await screen.findByLabelText('Variable name'), { target: { value: 'TOKEN' } });
    fireEvent.change(screen.getByLabelText('New value'), { target: { value: 'private' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save variable' }));
    expect(await screen.findByText(copy)).toBeVisible();
  });
  it('denies admin pages for owners without fetching any managed app', async () => {
    vi.spyOn(api, 'session').mockResolvedValue({
      role: 'owner',
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
  });
  it('lists every app for staff with create and adopt actions', async () => {
    mockSession(null, 'staff');
    vi.spyOn(api, 'session').mockResolvedValue(session!);
    const list = vi.spyOn(adminAppsApi, 'list').mockResolvedValue({
      items: [
        {
          applicationId: id,
          slug: 'class-fixture',
          ownerId: id,
          ownerUsername: 'student',
          ownerDisplayName: 'Student',
          savedRevision: 7,
          lifecycleState: 'ready',
          url: null,
          lastDeployedAt: null,
        },
      ],
      nextCursor: null,
      truncated: false,
    });
    show(<App />);
    expect(await screen.findByRole('link', { name: 'class-fixture' })).toHaveAttribute(
      'href',
      `/admin/apps/${id}`,
    );
    expect(list).toHaveBeenCalled();
    expect(screen.getByRole('heading', { name: 'All apps' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Create app' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Adopt app' })).toBeVisible();
  });
  it('lets staff manage ownership, deletion, outage and sizing', async () => {
    offline();
    mockSession(null, 'staff');
    vi.spyOn(adminAppsApi, 'detail').mockResolvedValue({
      ...detail,
      identityProvider: false,
      requiresMaintenance: false,
    });
    mockResources([
      {
        resourceId: '00000000-0000-4000-8000-0000000000aa',
        type: 'postgres' as const,
        label: 'main',
        status: 'ready',
        createdAt: new Date().toISOString(),
        verifiedAt: null,
        defaultBindings: {},
      },
    ]);
    const deploy = vi
      .spyOn(adminAppsApi, 'deploy')
      .mockResolvedValue({ intentId: id, state: 'accepted' } as never);
    vi.spyOn(api, 'intent').mockResolvedValue({ intentId: id, state: 'accepted' } as never);
    try {
      show(<AdminAppsPages />, `/admin/apps/${id}`);
      expect(await screen.findByRole('button', { name: 'Stop app' })).toBeVisible();
      expect(screen.getByText('Danger zone')).toBeVisible();
      expect(screen.getByRole('button', { name: 'Change owner' })).toBeVisible();
      expect(screen.getByRole('button', { name: 'Delete' })).toBeVisible();
      fireEvent.click(screen.getByRole('button', { name: 'Deploy' }));
      const dialog = screen.getByRole('dialog', { name: 'Deploy class-fixture' });
      expect(within(dialog).getByLabelText('Deployment method')).toBeVisible();
      expect(within(dialog).getByLabelText('Sizing plan', { exact: false })).toBeVisible();
      fireEvent.change(within(dialog).getByLabelText('Commit'), {
        target: { value: 'a'.repeat(40) },
      });
      fireEvent.click(within(dialog).getByRole('button', { name: 'Deploy' }));
      await waitFor(() =>
        expect(deploy).toHaveBeenCalledWith(
          id,
          settings.revision,
          'a'.repeat(40),
          false,
          undefined,
          expect.any(String),
        ),
      );
    } finally {
      vi.unstubAllGlobals();
    }
  });
  it('lets staff deploy an app with a fixed IP address without a checkbox', async () => {
    offline();
    mockSession(null, 'staff');
    vi.spyOn(adminAppsApi, 'detail').mockResolvedValue({ ...detail, identityProvider: false });
    mockResources();
    try {
      show(<AdminAppsPages />, `/admin/apps/${id}`);
      fireEvent.click(await screen.findByRole('button', { name: 'Deploy' }));
      const dialog = screen.getByRole('dialog', { name: 'Deploy class-fixture' });
      expect(dialog).toHaveTextContent('This app keeps a fixed IP address');
      expect(within(dialog).queryByRole('checkbox')).not.toBeInTheDocument();
      expect(within(dialog).getByRole('button', { name: 'Deploy' })).toBeEnabled();
    } finally {
      vi.unstubAllGlobals();
    }
  });
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
  it('shows maintenance and sign-in information without consent gates', async () => {
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
    expect(dialog).toHaveTextContent('This app keeps a fixed IP address');
    expect(dialog).toHaveTextContent('Signing in to this portal depends on this app.');
    expect(within(dialog).queryByRole('checkbox')).not.toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Deploy' })).toBeEnabled();
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
  it.each(['staff', 'admin'] as const)(
    'lets %s search owners and create for them without step-up',
    async (role) => {
      mockSession(null, role);
      vi.spyOn(adminAppsApi, 'list').mockResolvedValue({
        items: [],
        nextCursor: null,
        truncated: false,
      });
      const candidate = {
        userId: '00000000-0000-4000-8000-000000000099',
        username: 'alice',
        displayName: 'Alice',
        role: 'owner' as const,
        enabled: true,
        status: 'active' as const,
      };
      const lookup = vi.spyOn(adminAppsApi, 'owners').mockResolvedValue({
        items: [candidate],
        nextCursor: null,
        truncated: false,
      });
      const accounts = vi.spyOn(adminApi, 'accounts');
      const reauthenticate = vi.spyOn(adminApi, 'reauthenticate');
      const create = vi.spyOn(adminAppsApi, 'create').mockResolvedValue(detail);
      vi.spyOn(adminAppsApi, 'detail').mockResolvedValue(detail);
      mockResources();
      show(<AdminAppsPages />);
      fireEvent.click(await screen.findByRole('button', { name: 'Create app' }));
      const dialog = screen.getByRole('dialog', { name: 'Create app' });
      fireEvent.change(within(dialog).getByLabelText('App name'), { target: { value: 'new-app' } });
      fireEvent.change(within(dialog).getByLabelText('Owner'), { target: { value: 'alice' } });
      fireEvent.click(await within(dialog).findByRole('radio', { name: /Alice/ }));
      fireEvent.click(within(dialog).getByRole('button', { name: 'Create app' }));
      await waitFor(() =>
        expect(create).toHaveBeenCalledWith('new-app', candidate.userId, expect.any(String)),
      );
      expect(lookup).toHaveBeenCalledWith('alice');
      expect(accounts).not.toHaveBeenCalled();
      expect(reauthenticate).not.toHaveBeenCalled();
    },
  );
  it('sends app-management requests without the retired identity confirmation field', async () => {
    const fetcher = vi
      .fn()
      .mockImplementation(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({ data: { applicationId: id, intentId: id, state: 'accepted' } }),
            { status: 202 },
          ),
        ),
      );
    vi.stubGlobal('fetch', fetcher);
    try {
      await adminAppsApi.adopt(id, undefined, 'key');
      await adminAppsApi.reassign(id, id, id);
      await adminAppsApi.state(id, false, 'key');
      await adminAppsApi.deploy(id, 7, 'a'.repeat(40), false, undefined, 'key');
      await adminAppsApi.deleteStorage(id, id, 'class-fixture postgres', 'key');
      for (const [, init] of fetcher.mock.calls) {
        expect(JSON.parse(init.body)).not.toHaveProperty('identityProviderConfirmed');
      }
    } finally {
      vi.unstubAllGlobals();
    }
  });
  it.each(['staff', 'admin'] as const)('lets %s adopt without step-up or consent', async (role) => {
    mockSession(null, role);
    vi.spyOn(adminAppsApi, 'list').mockResolvedValue({
      items: [],
      nextCursor: null,
      truncated: false,
    });
    vi.spyOn(adminAppsApi, 'detail').mockResolvedValue(detail);
    mockResources();
    const reauthenticate = vi.spyOn(adminApi, 'reauthenticate');
    const adopt = vi.spyOn(adminAppsApi, 'adopt').mockResolvedValue({ applicationId: id });
    show(<AdminAppsPages />, '/admin/apps');
    fireEvent.click(await screen.findByRole('button', { name: 'Adopt app' }));
    const dialog = screen.getByRole('dialog', { name: 'Adopt app' });
    fireEvent.change(within(dialog).getByLabelText('App ID'), { target: { value: id } });
    expect(within(dialog).queryByRole('checkbox')).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Adopt app' }));
    await waitFor(() => expect(adopt).toHaveBeenCalledWith(id, undefined, expect.any(String)));
    expect(reauthenticate).not.toHaveBeenCalled();
  });
  it.each(['staff', 'admin'] as const)(
    'lets %s delete storage with typed confirmation and no step-up',
    async (role) => {
      mockSession(null, role);
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
          expect.any(String),
        ),
      );
    },
  );
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
