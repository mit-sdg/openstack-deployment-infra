import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { api, type AppRecord, type Intent, type Settings } from './api';
import { BuilderSizeControl } from './components/BuilderSize';
import { DeployPage } from './pages/Deploy';
import { ConfigurationPage } from './pages/Configuration';
import { PlatformSettingsPage } from './pages/PlatformSettings';
import { sizeLabel, sizingApi, workerSizeLabel, type ResizePlan } from './sizingApi';
import * as github from './utils/github';

const small = { flavor_id: '50', name: 'ups.1c1g', vcpus: 1, ram_mib: 1024, disk_gib: 20 };
const medium = { flavor_id: '100', name: 'ups.2c4g', vcpus: 2, ram_mib: 4096, disk_gib: 32 };
const large = { flavor_id: '200', name: 'ups.4c8g', vcpus: 4, ram_mib: 8192, disk_gib: 64 };
const settings: Settings = {
  revision: 1,
  repository: 'https://github.com/example/app',
  branch: 'main',
  configuration: {
    schemaVersion: 1,
    build: { runtime: 'node', packages: ['.'], buildScript: null, startScript: 'start' },
    runtime: { port: 3000, healthPath: '/health' },
    storageBindings: [],
  },
  configurationSha256: null,
};
const plan: ResizePlan = {
  applicationId: 'app',
  deploymentId: null,
  activation: 'enable-after-healthy-acceptance',
  current: { enabled: false, flavor: medium.name, cpuMHz: 500, memoryMiB: 512 },
  flavor: large,
  allocation: 'measured-worker-capacity-minus-reserve',
  reserve: { cpuMHzMinimum: 200, memoryMiBMinimum: 512, percentMinimum: 10 },
  fingerprint: 'a'.repeat(64),
};
const intent = (appId: string | null, kind: string): Intent => ({
  intentId: 'intent',
  appId,
  appSlug: appId ? 'demo' : null,
  kind,
  state: 'succeeded',
  operationId: null,
  operation: null,
  createdAt: new Date().toISOString(),
  safeError: null,
  commit: null,
});
function show(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={client}>{children}</QueryClientProvider>);
}
function deploy(access: 'owner' | 'admin', maintenance = false, choices = [small, medium, large]) {
  window.history.replaceState(null, '', '/apps/app/deploy?commit=' + 'a'.repeat(40));
  vi.spyOn(api, 'settings').mockResolvedValue(settings);
  vi.spyOn(api, 'environment').mockResolvedValue({ revision: 1, items: [], updatedAt: null });
  vi.spyOn(api, 'app').mockResolvedValue({
    applicationId: 'app',
    slug: 'demo',
    access,
    lifecycleState: 'ready',
    savedRevision: 1,
    desiredRunning: false,
    requiresMaintenance: maintenance,
    sizing: { workerFlavor: medium.name, cpuMHz: 500, memoryMiB: 512 },
    health: null,
    acceptedDeployment: null,
  } as AppRecord);
  vi.spyOn(api, 'sourceKey').mockResolvedValue({ present: false });
  vi.spyOn(github, 'recentCommits').mockResolvedValue([]);
  vi.spyOn(sizingApi, 'sizes').mockResolvedValue(choices);
  vi.spyOn(sizingApi, 'builder').mockResolvedValue({
    flavor: small,
    defaultFlavor: small,
    useDefault: true,
  });
  show(<DeployPage id="app" />);
}

describe('worker size', () => {
  it('labels and sorts sizes by vCPU then RAM', async () => {
    const sameCpu = { ...large, flavor_id: 'other', vcpus: 2 };
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify({ data: { items: [large, sameCpu, medium, small] } })),
        ),
    );
    expect((await sizingApi.sizes('app')).map((size) => size.flavor_id)).toEqual([
      '50',
      '100',
      'other',
      '200',
    ]);
    expect(sizeLabel(medium)).toBe('2 vCPU · 4 GB RAM · 32 GB disk (ups.2c4g)');
  });
  it('owners get neither size control nor sizing requests', async () => {
    deploy('owner');
    await screen.findByLabelText('Commit SHA');
    expect(screen.queryByLabelText('Worker size')).toBeNull();
    expect(screen.queryByLabelText('Build machine')).toBeNull();
    expect(sizingApi.sizes).not.toHaveBeenCalled();
    expect(sizingApi.builder).not.toHaveBeenCalled();
  });
  it('groups worker choices alphabetically by family and sorts each group by CPU and RAM', async () => {
    const choices = [
      { ...large, flavor_id: 'worker', name: 'worker-small' },
      { ...large, flavor_id: 'xl', name: 'xl.4c8g' },
      large,
      small,
      medium,
      { ...large, flavor_id: 'ups-2c8g', name: 'ups.2c8g', vcpus: 2 },
      { ...medium, flavor_id: 'lg', name: 'lg.2c4g' },
    ];
    deploy('admin', false, choices);
    const select = await screen.findByLabelText('Worker size');
    await within(select).findByRole('option', { name: /xl.4c8g/ });
    expect([...select.querySelectorAll('optgroup')].map((group) => group.label)).toEqual([
      'lg',
      'Other',
      'ups',
      'xl',
    ]);
    expect(
      [...select.querySelector('optgroup[label="ups"]')!.querySelectorAll('option')].map(
        (option) => option.value,
      ),
    ).toEqual(['50', '100', 'ups-2c8g', '200']);
  });
  it('keeps the build machine read-only in the deployment summary', async () => {
    deploy('admin');
    expect(await screen.findByText('1 vCPU · 1 GB · platform default')).toBeVisible();
    expect(screen.getByText('Build machine')).toBeVisible();
    expect(screen.queryByLabelText('Build machine')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
  });
  it('keeps the current size by default without a JSON field or plan request', async () => {
    const read = vi.spyOn(sizingApi, 'plan');
    deploy('admin');
    expect(await screen.findByLabelText('Worker size')).toHaveValue('');
    expect(
      await screen.findByRole('option', { name: `Keep current size — ${workerSizeLabel(medium)}` }),
    ).toBeInTheDocument();
    expect(await screen.findByRole('option', { name: /ups.2c4g.*current/ })).toBeDisabled();
    expect(screen.queryByLabelText('Sizing plan')).toBeNull();
    expect(read).not.toHaveBeenCalled();
  });
  it('fetches the plan, explains replacement and maintenance, and deploys it unchanged', async () => {
    const read = vi.spyOn(sizingApi, 'plan').mockResolvedValue(plan);
    const sent: unknown[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (path: string, options?: RequestInit) => {
        if (path.endsWith('/deployments') && options?.method === 'POST') {
          sent.push(JSON.parse(String(options.body)));
          return new Response(JSON.stringify({ data: intent('app', 'deploy') }));
        }
        return new Response('{}', { status: 404 });
      }),
    );
    deploy('admin', true);
    await within(await screen.findByLabelText('Worker size')).findByRole('option', {
      name: sizeLabel(large),
    });
    fireEvent.change(screen.getByLabelText('Worker size'), { target: { value: '200' } });
    await waitFor(() => expect(read).toHaveBeenCalledWith('app', '200'));
    expect(await screen.findByText(/The app can use up to 7.2 GB/)).toBeVisible();
    expect(screen.getByText(/This app keeps a fixed IP address/)).toBeVisible();
    expect(screen.queryByLabelText('Deployment method')).toBeNull();
    expect(screen.getByText('Now')).toBeVisible();
    expect(screen.getByText('After deploy')).toBeVisible();
    expect(screen.getByText(workerSizeLabel(medium))).toBeVisible();
    expect(screen.queryByText(/MHz|MiB|%/)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Review deployment' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Deploy' }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toEqual({
      configurationRevision: 1,
      commit: 'a'.repeat(40),
      maintenance: true,
      plan,
    });
  });
  it('ignores a late plan response after choosing another size', async () => {
    let finish: (value: ResizePlan) => void = () => {};
    vi.spyOn(sizingApi, 'plan').mockImplementation(async (_id, selected) =>
      selected === '200'
        ? new Promise<ResizePlan>((resolve) => {
            finish = resolve;
          })
        : { ...plan, flavor: small },
    );
    deploy('admin');
    const control = await screen.findByLabelText('Worker size');
    await within(control).findByRole('option', { name: sizeLabel(large) });
    fireEvent.change(control, { target: { value: '200' } });
    await waitFor(() => expect(sizingApi.plan).toHaveBeenCalledWith('app', '200'));
    expect(screen.getByRole('button', { name: 'Review deployment' })).toBeDisabled();
    fireEvent.change(control, { target: { value: '50' } });
    await screen.findByText(/The app can use up to 0.5 GB/);
    finish(plan);
    await waitFor(() => expect(screen.getByLabelText('Worker size')).toHaveValue('50'));
    expect(screen.queryByText(/The app can use up to 7.2 GB/)).toBeNull();
  });
  it('blocks deployment while the chosen plan loads or fails and clears it on Keep current', async () => {
    vi.spyOn(sizingApi, 'plan').mockRejectedValue(new Error('Could not read size'));
    deploy('admin');
    await within(await screen.findByLabelText('Worker size')).findByRole('option', {
      name: sizeLabel(large),
    });
    fireEvent.change(screen.getByLabelText('Worker size'), { target: { value: '200' } });
    await screen.findByText(/Couldn.t load the selected size/);
    expect(screen.getByRole('button', { name: 'Review deployment' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Worker size'), { target: { value: '' } });
    expect(screen.getByRole('button', { name: 'Review deployment' })).toBeEnabled();
  });
});

describe('builder sizes', () => {
  it('saves an app override independently and supports resetting it to default', async () => {
    let current = { flavor: large, defaultFlavor: small, useDefault: false };
    vi.spyOn(sizingApi, 'builder').mockImplementation(async () => current);
    const save = vi.spyOn(sizingApi, 'setBuilder').mockImplementation(async () => {
      current = { flavor: small, defaultFlavor: small, useDefault: true };
      return intent('app', 'builder_size');
    });
    vi.spyOn(api, 'intent').mockResolvedValue(intent('app', 'builder_size'));
    vi.spyOn(sizingApi, 'sizes').mockResolvedValue([small, medium, large]);
    show(<BuilderSizeControl id="app" />);
    expect(await screen.findByLabelText('Build machine')).toHaveValue('200');
    expect(screen.getByRole('option', { name: 'Platform default (1 vCPU · 1 GB)' })).toBeVisible();
    expect(screen.getByText('Set for this app')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Use platform default' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(save).toHaveBeenCalledWith(
        'app',
        null,
        large.name,
        expect.stringMatching(/^[a-f0-9-]{36}$/),
      ),
    );
    expect(await screen.findByText('Build machine saved.')).toBeVisible();
    expect(screen.queryByRole('list', { name: 'Build machine change' })).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });
  it.each(['owner', 'admin'] as const)(
    'puts the build-machine card on Settings only for elevated access (%s)',
    async (access) => {
      vi.spyOn(api, 'app').mockResolvedValue({
        applicationId: 'app',
        access,
        slug: 'demo',
      } as AppRecord);
      vi.spyOn(api, 'settings').mockResolvedValue(settings);
      vi.spyOn(api, 'environment').mockResolvedValue({ revision: 1, items: [], updatedAt: null });
      vi.spyOn(api, 'storage').mockResolvedValue({ items: [], intents: [] });
      vi.spyOn(api, 'sourceKey').mockResolvedValue({ present: false });
      const read = vi
        .spyOn(sizingApi, 'builder')
        .mockResolvedValue({ flavor: small, defaultFlavor: small, useDefault: true });
      vi.spyOn(sizingApi, 'sizes').mockResolvedValue([small, medium, large]);
      show(<ConfigurationPage id="app" />);
      await screen.findByLabelText('Build script');
      if (access === 'admin') {
        const card = await screen.findByRole('region', { name: 'Build machine settings' });
        expect(within(card).getByLabelText('Build machine')).toHaveValue('');
        expect(card.closest('form')).toBeNull();
        expect(within(card).getByRole('button', { name: 'Save' })).toBeDisabled();
      } else {
        expect(screen.queryByRole('region', { name: 'Build machine settings' })).toBeNull();
        expect(read).not.toHaveBeenCalled();
      }
    },
  );
  it('shows and changes the admin default with the same size labels', async () => {
    vi.spyOn(sizingApi, 'defaultBuilder').mockResolvedValue({
      flavor: small,
      sizes: [small, medium, large],
    });
    const save = vi
      .spyOn(sizingApi, 'setDefaultBuilder')
      .mockResolvedValue(intent(null, 'default_builder_size'));
    vi.spyOn(api, 'intent').mockResolvedValue(intent(null, 'default_builder_size'));
    show(<PlatformSettingsPage />);
    expect(await screen.findByLabelText('Default build machine')).toHaveValue('50');
    fireEvent.change(screen.getByLabelText('Default build machine'), { target: { value: '100' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(save).toHaveBeenCalledWith(
        '100',
        small.name,
        expect.stringMatching(/^[a-f0-9-]{36}$/),
      ),
    );
    expect(await screen.findByText('Default build machine saved.')).toBeVisible();
    expect(screen.queryByRole('list', { name: 'Default build machine change' })).toBeNull();
    expect(screen.queryByLabelText(/Admin.*size/)).toBeNull();
  });
});
