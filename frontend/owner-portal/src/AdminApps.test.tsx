import type { ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { api, request, record, type Session, type Settings } from './api';
import { adminAppsApi } from './adminAppsApi';
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
function show(node: ReactNode, path = '/admin/apps') {
  window.history.replaceState(null, '', path);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
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
        await screen.findByRole('heading', { name: 'Admin access unavailable' }),
      ).toBeVisible();
      expect(list).not.toHaveBeenCalled();
      expect(screen.queryByRole('link', { name: 'Manage applications' })).not.toBeInTheDocument();
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
    vi.spyOn(adminAppsApi, 'detail').mockResolvedValue({
      applicationId: id,
      slug: 'class-fixture',
      ownerId: id,
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
    });
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
    fireEvent.click(await screen.findByRole('button', { name: 'Deploy application' }));
    expect(screen.getByRole('dialog')).toHaveTextContent(
      'A retained primary IPv4 requires maintenance.',
    );
    expect(screen.getByRole('button', { name: 'Confirm deployment' })).toBeDisabled();
    fireEvent.click(screen.getByLabelText('Allow maintenance cutover'));
    expect(screen.getByRole('button', { name: 'Confirm deployment' })).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/confirm deployment/));
    expect(screen.getByRole('button', { name: 'Confirm deployment' })).toBeEnabled();
    expect(screen.getByRole('dialog')).toHaveTextContent('current accepted sizing is preserved');
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
    fireEvent.click(screen.getByRole('button', { name: 'Add or replace variable' }));
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
