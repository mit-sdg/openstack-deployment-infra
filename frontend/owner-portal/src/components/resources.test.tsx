import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { useState } from 'react';
import {
  ActionCanceled,
  api,
  resourceApi,
  validateBindings,
  validateEnvName,
  type Intent,
  type StorageBinding,
  type StorageResource,
} from '../api';
import { EnvironmentSection } from './EnvironmentSection';
import { StorageSection } from './StorageSection';
import { DeployPage } from '../pages/Deploy';

const intent: Intent = {
  intentId: 'intent',
  appId: 'app',
  appSlug: 'example',
  commit: null,
  createdAt: '2026-10-01T00:00:00Z',
  kind: 'env_set',
  state: 'succeeded',
  operationId: 'operation',
  operation: null,
  safeError: null,
};
const resource: StorageResource = {
  resourceId: 'resource',
  type: 'postgres',
  label: 'PostgreSQL',
  status: 'ready',
  createdAt: '2026-10-01T00:00:00Z',
  verifiedAt: null,
  defaultBindings: { url: 'DATABASE_URL', host: 'PGHOST' },
};
function wrap(children: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    client,
    ...render(<QueryClientProvider client={client}>{children}</QueryClientProvider>),
  };
}
function mocks() {
  vi.spyOn(api, 'intent').mockResolvedValue(intent);
  vi.spyOn(api, 'environment').mockResolvedValue({
    revision: 1,
    updatedAt: null,
    items: [{ name: 'API_TOKEN', updatedAt: null }],
  });
  vi.spyOn(api, 'storage').mockResolvedValue({ items: [resource], intents: [] });
}
describe('owner resources', () => {
  it('rejects reserved, invalid, duplicate and colliding binding names', () => {
    for (const name of ['PORT', 'STORAGE__SECRET', 'lower', 'A'.repeat(129)])
      expect(validateEnvName(name)).not.toBeNull();
    expect(
      validateBindings([{ resourceId: 'resource', outputs: { url: 'DB', host: 'DB' } }], []),
    ).toContain('more than one');
    expect(
      validateBindings([{ resourceId: 'resource', outputs: { url: 'DB' } }], ['DB']),
    ).toContain('already exists');
    expect(
      validateBindings([{ resourceId: 'resource', outputs: { url: 'APP_DATABASE' } }], []),
    ).toBeNull();
  });
  it('writes and clears a value without putting it in mutation variables', async () => {
    mocks();
    const set = vi.spyOn(api, 'setEnvironment').mockResolvedValue(intent);
    const { client } = wrap(<EnvironmentSection id="app" bindings={[]} />);
    fireEvent.change(screen.getByLabelText('Variable name'), { target: { value: 'NEW_TOKEN' } });
    fireEvent.change(screen.getByLabelText('New value'), { target: { value: 'SECRET_SENTINEL' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save variable' }));
    await waitFor(() => expect(set).toHaveBeenCalledOnce());
    expect(set.mock.calls[0].slice(0, 3)).toEqual(['app', 'NEW_TOKEN', 'SECRET_SENTINEL']);
    expect(screen.getByLabelText('New value')).toHaveValue('');
    expect(screen.queryByText('SECRET_SENTINEL')).not.toBeInTheDocument();
    expect(
      JSON.stringify(
        client
          .getMutationCache()
          .getAll()
          .map((m) => m.state.variables),
      ),
    ).not.toContain('SECRET_SENTINEL');
  });
  it('recovers an unknown edit after reload using names and its original key', async () => {
    mocks();
    const unknown = {
      ...intent,
      state: 'unknown',
      requiresResubmit: true,
      names: ['TOKEN'],
      retryKey: 'original-key',
    };
    vi.mocked(api.environment).mockResolvedValue({
      revision: 0,
      updatedAt: null,
      items: [],
      intents: [unknown],
    });
    vi.mocked(api.intent).mockResolvedValue(unknown);
    const write = vi.spyOn(api, 'setEnvironment').mockResolvedValue(intent);
    wrap(<EnvironmentSection id="app" bindings={[]} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Finish change to TOKEN' }));
    expect(screen.getByLabelText('Variable name')).toHaveValue('TOKEN');
    expect(screen.getByLabelText('New value')).toHaveValue('');
    fireEvent.change(screen.getByLabelText('New value'), {
      target: { value: 'resubmitted-secret' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save variable' }));
    await waitFor(() =>
      expect(write).toHaveBeenCalledWith('app', 'TOKEN', 'resubmitted-secret', 'original-key'),
    );
  });
  it('requires confirmation before deleting an environment name', async () => {
    mocks();
    const remove = vi
      .spyOn(api, 'deleteEnvironment')
      .mockResolvedValue({ ...intent, kind: 'env_delete' });
    const confirm = vi.spyOn(window, 'confirm');
    wrap(<EnvironmentSection id="app" bindings={[]} />);
    const button = await screen.findByRole('button', { name: 'Delete API_TOKEN' });
    fireEvent.click(button);
    expect(screen.getByRole('dialog')).toHaveTextContent('Delete API_TOKEN?');
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(remove).not.toHaveBeenCalled();
    fireEvent.click(button);
    fireEvent.click(screen.getByRole('button', { name: 'Delete variable' }));
    await waitFor(() => expect(remove).toHaveBeenCalledOnce());
    expect(remove.mock.calls[0].slice(0, 2)).toEqual(['app', 'API_TOKEN']);
    expect(confirm).not.toHaveBeenCalled();
  });
  it('disables existing types, allows renamed/partial bindings and warns before rotation', async () => {
    mocks();
    const rotate = vi
      .spyOn(api, 'storageAction')
      .mockResolvedValue({ ...intent, kind: 'storage_rotate' });
    const confirm = vi.spyOn(window, 'confirm');
    let current: StorageBinding[] = [];
    function Editor() {
      const [bindings, setBindings] = useState<StorageBinding[]>([]);
      current = bindings;
      return <StorageSection id="app" bindings={bindings} onChange={setBindings} />;
    }
    wrap(<Editor />);
    // Without variables, the row says the app can't connect and offers both paths.
    expect(await screen.findByText(/can’t connect to PostgreSQL yet/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Use default PostgreSQL variables' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Choose PostgreSQL variable names' }));
    expect(screen.queryByRole('button', { name: 'Add PostgreSQL' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add MongoDB' })).toBeEnabled();
    // The editor starts from the defaults.
    expect(screen.getByLabelText('host → environment name')).toHaveValue('PGHOST');
    fireEvent.change(screen.getByLabelText('url → environment name'), {
      target: { value: 'APP_DATABASE' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Remove host' }));
    expect(current).toEqual([]);
    fireEvent.click(screen.getByRole('button', { name: 'Save variables' }));
    await waitFor(() =>
      expect(current).toEqual([{ resourceId: 'resource', outputs: { url: 'APP_DATABASE' } }]),
    );
    expect(screen.getByRole('button', { name: 'Edit PostgreSQL variables' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Rotate PostgreSQL credentials' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('next deploy');
    expect(rotate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Rotate credentials' }));
    await waitFor(() => expect(rotate).toHaveBeenCalledOnce());
    expect(rotate.mock.calls[0].slice(0, 3)).toEqual(['app', 'resource', 'rotate']);
    expect(confirm).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /delete/i })).not.toBeInTheDocument();
    expect(screen.getByText(/Only an admin can delete/)).toBeVisible();
  });
  it('requires Commons confirmation before owner deployment', async () => {
    Object.defineProperty(HTMLDialogElement.prototype, 'close', {
      configurable: true,
      value: vi.fn(),
    });
    Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
      configurable: true,
      value: vi.fn(function (this: HTMLDialogElement) {
        this.setAttribute('open', '');
      }),
    });
    mocks();
    vi.spyOn(api, 'app').mockResolvedValue({
      applicationId: 'app',
      slug: 'commons',
      identityProvider: true,
      url: null,
      savedRevision: 7,
      lifecycleState: 'ready',
      desiredRunning: true,
      activeDeploymentId: null,
      acceptedDeployment: null,
      health: null,
      stale: false,
      observedAt: null,
    });
    vi.spyOn(api, 'settings').mockResolvedValue({
      revision: 7,
      repository: 'https://github.com/example/app',
      branch: 'main',
      configurationSha256: null,
      configuration: {
        schemaVersion: 1,
        build: { runtime: 'node', packages: ['.'], buildScript: null, startScript: 'start' },
        runtime: { port: 3000, healthPath: '/health' },
        storageBindings: [],
      },
    });
    const deployment = vi.spyOn(api, 'deploy').mockResolvedValue(intent);
    wrap(<DeployPage id="app" />);
    fireEvent.change(await screen.findByLabelText('Commit SHA'), {
      target: { value: 'a'.repeat(40) },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Review deployment' }));
    const submit = screen.getByRole('button', { name: 'Deploy' });
    expect(submit).toBeDisabled();
    expect(deployment).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText(/Signing in to this portal depends on this app/));
    fireEvent.click(submit);
    await waitFor(() =>
      expect(deployment).toHaveBeenCalledWith('app', 7, 'a'.repeat(40), expect.any(String), true),
    );
  });
  it('deploys a recent commit picked from GitHub', async () => {
    mocks();
    vi.spyOn(api, 'app').mockResolvedValue({ identityProvider: false } as never);
    vi.spyOn(api, 'settings').mockResolvedValue({
      revision: 3,
      repository: 'https://github.com/example/app',
      branch: 'main',
      configurationSha256: null,
      configuration: {
        schemaVersion: 1,
        build: { runtime: 'node', packages: ['.'], buildScript: null, startScript: 'start' },
        runtime: { port: 3000, healthPath: '/health' },
        storageBindings: [],
      },
    });
    const commits = ['b', 'c'].map((digit, index) => ({
      sha: digit.repeat(40),
      commit: {
        message: `Change ${index + 1}\n\nDetails`,
        author: { name: 'Ada', date: '2026-10-03T12:00:00Z' },
      },
    }));
    const github = vi.fn((url: string) =>
      Promise.resolve(
        new Response(
          url.includes('/commits?')
            ? JSON.stringify(commits)
            : url.includes('/git/trees/')
              ? JSON.stringify({
                  tree: [
                    { path: 'package.json', type: 'blob', mode: '100644', size: 30 },
                    { path: 'package-lock.json', type: 'blob', mode: '100644', size: 2 },
                  ],
                })
              : '{"scripts":{"start":"node ."}}',
        ),
      ),
    );
    vi.stubGlobal('fetch', github);
    const deployment = vi.spyOn(api, 'deploy').mockResolvedValue(intent);
    wrap(<DeployPage id="app" />);
    const group = await screen.findByRole('group', { name: 'Recent commits on main' });
    fireEvent.click(await within(group).findByRole('radio', { name: 'Change 2' }));
    expect(screen.getByLabelText('Commit SHA')).toHaveValue('c'.repeat(40));
    expect(
      await screen.findByText(
        'This commit has the package.json, scripts and lockfile the build needs.',
      ),
    ).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Review deployment' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('Change 2');
    fireEvent.click(screen.getByRole('button', { name: 'Deploy' }));
    await waitFor(() =>
      expect(deployment).toHaveBeenCalledWith('app', 3, 'c'.repeat(40), expect.any(String), false),
    );
    expect(github.mock.calls.map(([url]) => new URL(url).pathname)).toEqual([
      '/repos/example/app/commits',
      '/repos/example/app/git/trees/' + 'c'.repeat(40),
      '/example/app/' + 'c'.repeat(40) + '/package.json',
    ]);
  });
  it('confirms sign-in app storage changes through an async callback, never window.confirm', async () => {
    const app = vi.spyOn(api, 'app').mockResolvedValue({ identityProvider: true } as never);
    const native = vi.spyOn(window, 'confirm');
    const fetcher = vi
      .fn()
      .mockImplementation(() =>
        Promise.resolve(new Response(JSON.stringify({ data: intent }), { status: 202 })),
      );
    vi.stubGlobal('fetch', fetcher);
    try {
      // Without a callback, the sign-in app's storage can't change and nothing is sent.
      await expect(api.createStorage('app', 'postgres', 'key')).rejects.toThrow(
        'Portal sign-in depends on this app',
      );
      let answer = false;
      const confirm = vi.fn(() => Promise.resolve(answer));
      const owner = resourceApi('/apps', confirm);
      await expect(owner.createStorage('app', 'postgres', 'key')).rejects.toBeInstanceOf(
        ActionCanceled,
      );
      expect(fetcher).not.toHaveBeenCalled();
      answer = true;
      await owner.createStorage('app', 'postgres', 'key');
      expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({
        type: 'postgres',
        identityProviderConfirmed: true,
      });
      for (const action of ['verify', 'rotate'] as const) {
        await owner.storageAction('app', 'resource', action, 'key');
        expect(JSON.parse(fetcher.mock.lastCall![1].body)).toEqual({
          identityProviderConfirmed: true,
        });
      }
      // Other owner apps are not asked and send no confirmation field.
      app.mockResolvedValue({ identityProvider: false } as never);
      confirm.mockClear();
      await owner.storageAction('app', 'resource', 'verify', 'key');
      expect(confirm).not.toHaveBeenCalled();
      expect(JSON.parse(fetcher.mock.lastCall![1].body)).toEqual({});
      // Admin requests let the callback decide, as before.
      await resourceApi('/admin-apps', () => Promise.resolve(true)).storageAction(
        'app',
        'resource',
        'rotate',
        'key',
      );
      expect(JSON.parse(fetcher.mock.lastCall![1].body)).toEqual({
        identityProviderConfirmed: true,
      });
      await resourceApi('/admin-apps').storageAction('app', 'resource', 'rotate', 'key');
      expect(JSON.parse(fetcher.mock.lastCall![1].body)).toEqual({});
      expect(native).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });
  it('confirms owner storage changes for the sign-in app in a dialog, not a native prompt', async () => {
    mocks();
    vi.spyOn(api, 'app').mockResolvedValue({ identityProvider: true } as never);
    const confirm = vi.spyOn(window, 'confirm');
    const fetcher = vi
      .fn()
      .mockImplementation(() =>
        Promise.resolve(new Response(JSON.stringify({ data: intent }), { status: 202 })),
      );
    vi.stubGlobal('fetch', fetcher);
    try {
      wrap(<StorageSection id="app" bindings={[]} onChange={() => {}} />);
      await waitFor(() => expect(api.app).toHaveBeenCalled());
      fireEvent.click(await screen.findByRole('button', { name: 'Add MongoDB' }));
      const submit = screen.getAllByRole('button', { name: 'Add MongoDB' }).at(-1)!;
      expect(submit).toBeDisabled();
      fireEvent.click(screen.getByLabelText(/Signing in to this portal depends on this app/));
      fireEvent.click(submit);
      await waitFor(() => expect(fetcher).toHaveBeenCalledOnce());
      expect(fetcher.mock.calls[0][0]).toBe('/api/v1/apps/app/storage');
      expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({
        type: 'mongo',
        identityProviderConfirmed: true,
      });
      expect(confirm).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });
  it('confirms admin storage changes for the sign-in app in the same dialog', async () => {
    vi.spyOn(api, 'intent').mockResolvedValue(intent);
    // The admin page passes a confirming service only for the sign-in app;
    // the section's dialog must collect consent before any request.
    const service = resourceApi('/admin-apps', () => true);
    vi.spyOn(service, 'environment').mockResolvedValue({ revision: 1, updatedAt: null, items: [] });
    vi.spyOn(service, 'storage').mockResolvedValue({ items: [], intents: [] });
    const confirm = vi.spyOn(window, 'confirm');
    const fetcher = vi
      .fn()
      .mockImplementation(() =>
        Promise.resolve(new Response(JSON.stringify({ data: intent }), { status: 202 })),
      );
    vi.stubGlobal('fetch', fetcher);
    try {
      wrap(
        <StorageSection
          id="app"
          service={service}
          identityProvider
          bindings={[]}
          onChange={() => {}}
        />,
      );
      fireEvent.click(await screen.findByRole('button', { name: 'Add MongoDB' }));
      const submit = screen.getAllByRole('button', { name: 'Add MongoDB' }).at(-1)!;
      expect(submit).toBeDisabled();
      expect(fetcher).not.toHaveBeenCalled();
      fireEvent.click(screen.getByLabelText(/Signing in to this portal depends on this app/));
      fireEvent.click(submit);
      await waitFor(() => expect(fetcher).toHaveBeenCalledOnce());
      expect(fetcher.mock.calls[0][0]).toBe('/api/v1/admin-apps/app/storage');
      expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({
        type: 'mongo',
        identityProviderConfirmed: true,
      });
      expect(confirm).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });
  it('allows PostgreSQL password and S3 secret-key bindings', async () => {
    mocks();
    vi.mocked(api.storage).mockResolvedValue({
      items: [
        { ...resource, defaultBindings: { url: 'DATABASE_URL', password: 'PGPASSWORD' } },
        {
          ...resource,
          resourceId: 'bucket',
          type: 's3',
          label: 'S3',
          defaultBindings: { secret_access_key: 'AWS_SECRET_ACCESS_KEY' },
        },
      ],
      intents: [],
    });
    function Editor() {
      const [bindings, setBindings] = useState<StorageBinding[]>([]);
      return <StorageSection id="app" bindings={bindings} onChange={setBindings} />;
    }
    wrap(<Editor />);
    fireEvent.click(
      await screen.findByRole('button', { name: 'Choose PostgreSQL variable names' }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Remove password' }));
    fireEvent.click(screen.getByRole('button', { name: 'Add password as PGPASSWORD' }));
    expect(screen.getByLabelText('password → environment name')).toHaveValue('PGPASSWORD');
    fireEvent.click(screen.getByRole('button', { name: 'Save variables' }));
    fireEvent.click(
      await screen.findByRole('button', { name: 'Choose S3 storage variable names' }),
    );
    expect(screen.getByLabelText('secret_access_key → environment name')).toHaveValue(
      'AWS_SECRET_ACCESS_KEY',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Save variables' }));
    expect(await screen.findByText('2 variables')).toBeVisible();
    expect(await screen.findByText('1 variable')).toBeVisible();
    expect(screen.queryByText(/needs a platform update/)).not.toBeInTheDocument();
  });
  it('shows only injected names on deploy review', async () => {
    Object.defineProperty(HTMLDialogElement.prototype, 'close', {
      configurable: true,
      value: vi.fn(),
    });
    mocks();
    vi.spyOn(api, 'app').mockResolvedValue({
      applicationId: 'app',
      slug: 'example',
      url: null,
      savedRevision: 1,
      lifecycleState: 'ready',
      desiredRunning: false,
      activeDeploymentId: null,
      acceptedDeployment: null,
      health: null,
      stale: false,
      observedAt: null,
    });
    vi.spyOn(api, 'settings').mockResolvedValue({
      revision: 1,
      repository: 'https://github.com/example/app',
      branch: 'main',
      configurationSha256: null,
      configuration: {
        schemaVersion: 1,
        build: { runtime: 'node', packages: ['.'], buildScript: null, startScript: 'start' },
        runtime: { port: 3000, healthPath: '/health' },
        storageBindings: [{ resourceId: 'resource', outputs: { url: 'APP_DATABASE' } }],
      },
    });
    wrap(<DeployPage id="app" />);
    expect(await screen.findByText('APP_DATABASE')).toBeVisible();
    expect(screen.getByText('API_TOKEN')).toBeVisible();
  });
});
