import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { api, type AppRecord } from '../api';
import { Overview } from '../pages/Overview';
import { sizingApi } from '../sizingApi';

const flavor = { flavor_id: '50', name: 'ups.1c1g', vcpus: 1, ram_mib: 1024, disk_gib: 20 };
const app: AppRecord = {
  applicationId: 'app',
  slug: 'demo',
  ownerId: 'owner',
  access: 'admin',
  url: null,
  lifecycleState: 'ready',
  savedRevision: 0,
  desiredRunning: true,
  activeDeploymentId: null,
  acceptedDeployment: null,
  health: null,
  stale: false,
  observedAt: null,
  sizing: { workerFlavor: 'xl.4core', cpuMHz: 7916, memoryMiB: 14395, vcpus: 4, ram_mib: 16384 },
};
function show(record = app, useDefault = true) {
  vi.spyOn(api, 'app').mockResolvedValue(record);
  vi.spyOn(api, 'history').mockResolvedValue({ items: [], nextCursor: null, truncated: false });
  vi.spyOn(api, 'activity').mockResolvedValue([]);
  vi.spyOn(api, 'attention').mockResolvedValue([]);
  const read = vi
    .spyOn(sizingApi, 'builder')
    .mockResolvedValue({ flavor, defaultFlavor: flavor, useDefault });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <Overview id="app" />
    </QueryClientProvider>,
  );
  return read;
}
describe('Overview machines', () => {
  it.each([true, false])(
    'explains worker and build machine capacity (default=%s)',
    async (useDefault) => {
      show(app, useDefault);
      const section = await screen.findByRole('region', { name: 'Machines' });
      expect(within(section).getByText('4 vCPU · 16 GB RAM (xl.4core)')).toBeVisible();
      expect(
        within(section).getByText(
          'The app can use up to 14.1 GB of memory and all 4 vCPUs (about 2.0 GHz each).',
        ),
      ).toBeVisible();
      expect(
        await within(section).findByText(
          `1 vCPU · 1 GB RAM (ups.1c1g) · ${useDefault ? 'platform default' : 'set for this app'}`,
        ),
      ).toBeVisible();
      expect(within(section).getByRole('link', { name: 'Change size' })).toHaveAttribute(
        'href',
        '/apps/app/deploy#worker-size',
      );
      expect(within(section).getByRole('link', { name: 'Change' })).toHaveAttribute(
        'href',
        '/apps/app/configuration#builder-size',
      );
      expect(
        within(screen.getByRole('heading', { name: 'Owner' }).closest('section')!).queryByText(
          /GB|GHz/,
        ),
      ).toBeNull();
      expect(screen.queryByText('Size', { exact: true })).toBeNull();
    },
  );
  it('labels total CPU explicitly when flavor capacity is unavailable', async () => {
    show({ ...app, sizing: { workerFlavor: 'xl.4core', cpuMHz: 7916, memoryMiB: 14395 } });
    expect(
      await screen.findByText('xl.4core · 14.1 GB memory for the app · 7.9 GHz CPU in total'),
    ).toBeVisible();
    expect(screen.queryByText(/GHz each|7.916|14395 MB/)).toBeNull();
  });
  it.each(['owner', 'member'] as const)(
    'hides machines and avoids the sizing read for %s',
    async (access) => {
      const read = show({ ...app, access });
      await screen.findByRole('heading', { name: 'Set up your app' });
      expect(screen.queryByRole('region', { name: 'Machines' })).toBeNull();
      expect(read).not.toHaveBeenCalled();
    },
  );
});
