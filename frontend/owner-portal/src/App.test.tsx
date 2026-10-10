import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { api, ApiError, configurationGuidance, validateSettings, type Settings } from './api';
import { Status } from './components/Status';
import { DeployPage } from './pages/Deploy';
import * as github from './utils/github';
import { Overview } from './pages/Overview';
import { ConfigurationForm } from './pages/Configuration';

const settings: Settings = {
  revision: 1,
  repository: 'https://github.com/example/student-app',
  branch: 'main',
  configuration: {
    schemaVersion: 1,
    build: { runtime: 'node', packages: ['.'], buildScript: null, startScript: 'start' },
    runtime: { port: 3000, healthPath: '/health' },
    storageBindings: [],
  },
  configurationSha256: null,
};
describe('owner configuration', () => {
  it('accepts typed Node and Bun settings and rejects command text/path escapes', () => {
    expect(validateSettings(settings)).toBeNull();
    expect(
      validateSettings({
        ...settings,
        configuration: {
          ...settings.configuration,
          build: { ...settings.configuration.build, runtime: 'bun' },
        },
      }),
    ).toBeNull();
    for (const invalid of ['../escape', '/outside', 'a\\b'])
      expect(
        validateSettings({
          ...settings,
          configuration: {
            ...settings.configuration,
            build: { ...settings.configuration.build, packages: [invalid] },
          },
        }),
      ).not.toBeNull();
    expect(
      validateSettings({
        ...settings,
        configuration: {
          ...settings.configuration,
          build: { ...settings.configuration.build, startScript: 'node server.js' },
        },
      }),
    ).not.toBeNull();
    expect(
      validateSettings({
        ...settings,
        configuration: { ...settings.configuration, runtime: { port: 0, healthPath: '/health' } },
      }),
    ).not.toBeNull();
  });
  it('renders labeled editable controls and saves independently of deploy', async () => {
    const save = vi.spyOn(api, 'save').mockResolvedValue({ revision: 2 });
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ConfigurationForm id="app" initial={settings} />
      </QueryClientProvider>,
    );
    expect(screen.getByText(configurationGuidance.scripts)).toBeVisible();
    expect(screen.getByText(configurationGuidance.root, { exact: false })).toBeVisible();
    expect(screen.getByText(/Each needs its lockfile/)).toBeVisible();
    expect(screen.getByText(configurationGuidance.health)).toBeVisible();
    expect(screen.getByText(configurationGuidance.versions)).toBeVisible();
    fireEvent.click(screen.getByLabelText(/Bun/));
    fireEvent.change(screen.getByLabelText('Port'), { target: { value: '8080' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save settings' }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    expect(save.mock.calls[0][1].configuration.build.runtime).toBe('bun');
    expect(save.mock.calls[0][1].configuration.runtime.port).toBe(8080);
    expect(await screen.findByRole('status')).toHaveTextContent('Settings saved');
  });
  it('saves default database variables in one click without saving other draft edits', async () => {
    vi.spyOn(api, 'environment').mockResolvedValue({ revision: 0, updatedAt: null, items: [] });
    vi.spyOn(api, 'storage').mockResolvedValue({
      items: [
        {
          resourceId: 'db',
          type: 'postgres',
          label: 'PostgreSQL',
          status: 'ready',
          createdAt: '2026-10-01T00:00:00Z',
          verifiedAt: null,
          quotas: {
            connections: 10,
            sizeBytes: 2147483648,
            memoryBytes: 536870912,
            cpuMillicores: 500,
          },
          usage: {
            usedBytes: null,
            objectCount: null,
            currentConnections: null,
            instanceMemoryBytes: null,
            cpuTimeMilliseconds: null,
            measuredAt: null,
            stale: true,
          },
          writeBlock: { blocked: false, reason: null, since: null },
          isolation: 'instance',
          hardQuotaBytes: 2684354560,
          defaultBindings: { url: 'DATABASE_URL' },
        },
      ],
      intents: [],
    });
    vi.spyOn(api, 'app').mockResolvedValue({ identityProvider: false } as never);
    const save = vi.spyOn(api, 'save').mockResolvedValue({ revision: 2 });
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ConfigurationForm id="app" initial={settings} resources />
      </QueryClientProvider>,
    );
    fireEvent.change(screen.getByLabelText('Branch'), { target: { value: 'draft-branch' } });
    fireEvent.click(
      await screen.findByRole('button', { name: 'Use default PostgreSQL variables' }),
    );
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    const sent = save.mock.calls[0][1];
    expect(sent.branch).toBe('main');
    expect(sent.revision).toBe(1);
    expect(sent.configuration.storageBindings).toEqual([
      { resourceId: 'db', outputs: { url: 'DATABASE_URL' } },
    ]);
    expect(await screen.findByText('1 variable')).toBeVisible();
    // The draft keeps its unsaved edit.
    expect(screen.getByLabelText('Branch')).toHaveValue('draft-branch');
  });
  it('explains revision conflicts and preserves the draft', async () => {
    vi.spyOn(api, 'save').mockRejectedValue(
      new ApiError(409, 'REVISION_CONFLICT', 'Settings changed in another tab.'),
    );
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ConfigurationForm id="app" initial={settings} />
      </QueryClientProvider>,
    );
    fireEvent.change(screen.getByLabelText('Branch'), {
      target: { value: 'feature/student' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save settings' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Settings changed in another tab');
    expect(screen.getByLabelText('Branch')).toHaveValue('feature/student');
  });
  it('reports statuses using text in addition to color', () => {
    render(
      <>
        <Status state="unknown" />
        <Status state="blocked" />
      </>,
    );
    expect(screen.getByText('Unknown')).toBeVisible();
    expect(screen.getByText('Needs attention')).toBeVisible();
  });
});
describe('typed API', () => {
  it('rejects malformed responses', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ data: { applicationId: 12 } }),
      }),
    );
    await expect(api.app('app')).rejects.toThrow('Invalid service response');
  });
  it('rejects a session without the paired broker brand field', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({
          data: {
            csrfToken: 'csrf',
            expiresAt: '2026-10-01T12:00:00Z',
            role: 'owner',
            user: { id: 'u', displayName: 'Owner', username: 'owner' },
          },
        }),
      }),
    );
    await expect(api.session()).rejects.toThrow('Invalid service response');
  });
  it('refreshes expired CSRF once while retaining the same mutation key', async () => {
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
        json: async () => ({
          data: {
            csrfToken: 'replacement',
            platformName: 'Example platform',
            role: 'owner',
            stepUpExpiresAt: null,
            expiresAt: '2026-10-01T12:00:00Z',
            user: { id: 'u', displayName: 'Student', username: 'student' },
          },
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ data: { revision: 2 } }),
      });
    vi.stubGlobal('fetch', fetch);
    await api.save('app', settings, 'fixed-key');
    expect(fetch.mock.calls[0][1].headers['Idempotency-Key']).toBe('fixed-key');
    expect(fetch.mock.calls[2][1].headers['Idempotency-Key']).toBe('fixed-key');
    expect(fetch.mock.calls[2][1].headers['X-CSRF-Token']).toBe('replacement');
  });
});

describe('owner app controls', () => {
  it('refreshes the app as soon as a stop finishes', async () => {
    const app = {
      applicationId: 'app',
      slug: 'demo',
      savedRevision: 1,
      lifecycleState: 'ready',
      desiredRunning: true,
      stale: false,
      activeDeploymentId: 'deployment',
      acceptedDeployment: {
        deploymentId: 'deployment',
        sourceCommit: 'a'.repeat(40),
        acceptedAt: '2026-10-01T00:00:00Z',
      },
    };
    const read = vi.spyOn(api, 'app').mockResolvedValue(app as never);
    vi.spyOn(api, 'history').mockResolvedValue({ items: [], nextCursor: null, truncated: false });
    vi.spyOn(api, 'activity').mockResolvedValue([]);
    vi.spyOn(api, 'state').mockImplementation(async () => {
      read.mockResolvedValue({ ...app, desiredRunning: false } as never);
      return { intentId: 'stop', state: 'accepted' } as never;
    });
    vi.spyOn(api, 'intent').mockResolvedValue({
      intentId: 'stop',
      kind: 'app_disable',
      appId: 'app',
      state: 'succeeded',
    } as never);
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <Overview id="app" />
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Stop app' }));
    fireEvent.click(screen.getAllByRole('button', { name: 'Stop app' })[1]);
    expect(await screen.findByRole('button', { name: 'Start app' })).toBeEnabled();
  });

  it('confirms a restart and uses one idempotency key while preserving the version', async () => {
    const app = {
      applicationId: 'app',
      slug: 'demo',
      savedRevision: 1,
      lifecycleState: 'ready',
      desiredRunning: true,
      stale: false,
      activeDeploymentId: 'deployment',
      acceptedDeployment: {
        deploymentId: 'deployment',
        sourceCommit: 'a'.repeat(40),
        acceptedAt: '2026-10-01T00:00:00Z',
      },
    };
    vi.spyOn(api, 'app').mockResolvedValue(app as never);
    vi.spyOn(api, 'history').mockResolvedValue({ items: [], nextCursor: null, truncated: false });
    vi.spyOn(api, 'activity').mockResolvedValue([]);
    const restart = vi
      .spyOn(api, 'restart')
      .mockResolvedValue({ intentId: 'restart', state: 'accepted' } as never);
    vi.spyOn(api, 'intent').mockResolvedValue({
      intentId: 'restart',
      kind: 'app_restart',
      appId: 'app',
      state: 'succeeded',
    } as never);
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <Overview id="app" />
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Restart app' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('same server');
    expect(screen.getByRole('dialog')).toHaveTextContent('brief interruption');
    expect(restart).not.toHaveBeenCalled();
    fireEvent.click(screen.getAllByRole('button', { name: 'Restart app' })[1]);
    await waitFor(() => expect(restart).toHaveBeenCalledOnce());
    expect(restart.mock.calls[0]).toEqual(['app', expect.stringMatching(/^[a-f0-9-]{36}$/)]);
  });
});

describe('redeploy selection', () => {
  function show(search: string) {
    window.history.replaceState(null, '', `/apps/app/deploy${search}`);
    vi.spyOn(api, 'settings').mockResolvedValue(settings);
    vi.spyOn(api, 'app').mockResolvedValue({
      slug: 'demo',
      savedRevision: 1,
      lifecycleState: 'ready',
      desiredRunning: true,
    } as never);
    vi.spyOn(api, 'environment').mockResolvedValue({ revision: 1, items: [], updatedAt: null });
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(new Response('{}', { status: 404 }))),
    );
    return render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <DeployPage id="app" />
      </QueryClientProvider>,
    );
  }
  it('selects the exact old commit while explaining current settings', async () => {
    vi.spyOn(github, 'recentCommits').mockResolvedValue([]);
    show('?commit=' + 'a'.repeat(40));
    expect(await screen.findByLabelText('Commit SHA')).toHaveValue('a'.repeat(40));
    expect(
      screen.getByText(
        'Deploying this commit again uses your app’s current saved settings and environment variables.',
      ),
    ).toBeVisible();
    expect(screen.queryByRole('dialog')).toBeNull();
  });
  it.each([false, true])(
    'reviews the newest saved branch commit (private: %s)',
    async (privateRepo) => {
      const read = vi.spyOn(github, 'recentCommits');
      if (privateRepo) read.mockRejectedValue(new github.GitHubError('not-found'));
      else
        read.mockResolvedValue([
          { sha: 'b'.repeat(40), message: 'Newest', author: null, date: null },
        ]);
      vi.spyOn(api, 'sourceKey').mockResolvedValue({ present: true } as never);
      const platform = vi
        .spyOn(api, 'recentSourceCommits')
        .mockResolvedValue([{ sha: 'b'.repeat(40), message: 'Newest', author: null, date: null }]);
      const head = vi.spyOn(api, 'checkSourceKey');
      const deploy = vi.spyOn(api, 'deploy');
      show('?latest=1');
      expect(await screen.findByRole('dialog')).toHaveTextContent('b'.repeat(40));
      expect(screen.getByRole('dialog')).toHaveTextContent('current saved settings');
      expect(deploy).not.toHaveBeenCalled();
      expect(head).not.toHaveBeenCalled();
      if (privateRepo) expect(platform).toHaveBeenCalledWith('app');
      expect(read).toHaveBeenCalledWith(
        settings.repository,
        settings.branch,
        expect.any(AbortSignal),
      );
    },
  );
});
