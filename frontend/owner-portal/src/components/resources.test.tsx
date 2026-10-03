import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { useState } from 'react';
import {
  api,
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
    fireEvent.click(screen.getByRole('button', { name: 'Add or replace variable' }));
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
    fireEvent.click(await screen.findByRole('button', { name: 'Recover edit of TOKEN' }));
    expect(screen.getByLabelText('Variable name')).toHaveValue('TOKEN');
    expect(screen.getByLabelText('New value')).toHaveValue('');
    fireEvent.change(screen.getByLabelText('New value'), {
      target: { value: 'resubmitted-secret' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add or replace variable' }));
    await waitFor(() =>
      expect(write).toHaveBeenCalledWith('app', 'TOKEN', 'resubmitted-secret', 'original-key'),
    );
  });
  it('requires confirmation before deleting an environment name', async () => {
    mocks();
    const remove = vi
      .spyOn(api, 'deleteEnvironment')
      .mockResolvedValue({ ...intent, kind: 'env_delete' });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    wrap(<EnvironmentSection id="app" bindings={[]} />);
    const button = await screen.findByRole('button', { name: 'Delete API_TOKEN' });
    fireEvent.click(button);
    expect(remove).not.toHaveBeenCalled();
    confirm.mockReturnValue(true);
    fireEvent.click(button);
    await waitFor(() => expect(remove).toHaveBeenCalledOnce());
  });
  it('disables existing types, allows renamed/partial bindings and warns before rotation', async () => {
    mocks();
    const rotate = vi
      .spyOn(api, 'storageAction')
      .mockResolvedValue({ ...intent, kind: 'storage_rotate' });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    let current: StorageBinding[] = [];
    function Editor() {
      const [bindings, setBindings] = useState<StorageBinding[]>([]);
      current = bindings;
      return <StorageSection id="app" bindings={bindings} onChange={setBindings} />;
    }
    wrap(<Editor />);
    await screen.findByRole('button', { name: 'Use default bindings' });
    expect(screen.getByRole('button', { name: 'Add PostgreSQL' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Use default bindings' }));
    fireEvent.change(screen.getByLabelText('url → environment name'), {
      target: { value: 'APP_DATABASE' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Remove host binding' }));
    expect(current).toEqual([{ resourceId: 'resource', outputs: { url: 'APP_DATABASE' } }]);
    fireEvent.click(screen.getByRole('button', { name: 'Rotate PostgreSQL credentials' }));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('Redeploy'));
    await waitFor(() => expect(rotate).toHaveBeenCalledOnce());
    expect(screen.queryByRole('button', { name: /delete/i })).not.toBeInTheDocument();
    expect(screen.getByText(/Ask an administrator/)).toBeVisible();
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
