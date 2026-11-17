import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, type StorageResource } from '../api';
import { appManagementApi, type Snapshot } from '../appManagementApi';
import { StorageSection } from './StorageSection';
import { browser } from './StorageSnapshot';

const mongo: StorageResource = {
  resourceId: 'mongo-resource',
  type: 'mongo',
  label: 'MongoDB',
  status: 'ready',
  createdAt: '2026-11-01T00:00:00Z',
  verifiedAt: null,
  quotas: { connections: 10, sizeBytes: 2147483648, memoryBytes: 536870912, cpuMillicores: 500 },
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
  defaultBindings: { uri: 'MONGODB_URI' },
};
const bucket: StorageResource = {
  ...mongo,
  resourceId: 'bucket',
  type: 's3',
  label: 'S3 storage',
  defaultBindings: { bucket: 'S3_BUCKET' },
};
const preparing: Snapshot = {
  snapshotId: 'snapshot',
  resourceId: 'mongo-resource',
  state: 'preparing',
  startedAt: '2026-11-17T12:00:00Z',
  filename: null,
  bytes: null,
  sha256: null,
  takenAt: null,
  expiresAt: null,
  error: null,
};
const ready: Snapshot = {
  ...preparing,
  state: 'ready',
  filename: 'demo-app-mongodb-20261117T120000Z.zip',
  bytes: 3 * 1024 ** 2,
  sha256: 'c'.repeat(64),
  takenAt: '2026-11-17T12:00:00Z',
  expiresAt: '2026-11-18T12:00:00Z',
};

function show(access: 'owner' | 'admin') {
  vi.spyOn(api, 'storage').mockResolvedValue({ items: [mongo, bucket], intents: [] });
  vi.spyOn(api, 'environment').mockResolvedValue({ revision: 1, items: [], updatedAt: null });
  vi.spyOn(api, 'app').mockResolvedValue({ access } as Awaited<ReturnType<typeof api.app>>);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <StorageSection id="app" bindings={[]} onChange={() => {}} />
    </QueryClientProvider>,
  );
}

describe('database snapshots', () => {
  afterEach(() => vi.restoreAllMocks());

  it('are offered to staff for databases only, never to owners', async () => {
    show('owner');
    await screen.findByRole('button', { name: 'Verify MongoDB' });
    expect(screen.queryByRole('button', { name: /snapshot/ })).not.toBeInTheDocument();
  });

  it('warn first, then download each time through a fresh link', async () => {
    show('admin');
    fireEvent.click(await screen.findByRole('button', { name: 'Download a MongoDB snapshot' }));
    expect(screen.queryByRole('button', { name: 'Download a S3 storage snapshot' })).toBeNull();
    expect(screen.getByText(/including personal data/)).toBeVisible();
    expect(screen.getByText(/every download are logged/)).toBeVisible();
    const start = vi.spyOn(appManagementApi, 'requestSnapshot').mockResolvedValue(preparing);
    const status = vi
      .spyOn(appManagementApi, 'snapshot')
      .mockResolvedValueOnce(preparing)
      .mockResolvedValue(ready);
    const link = vi.spyOn(appManagementApi, 'snapshotLink').mockResolvedValue({
      url: 'https://s3.example.test/exports/file.zip?signature',
      expiresAt: '2026-11-17T12:05:00Z',
      filename: ready.filename!,
      bytes: ready.bytes!,
      sha256: ready.sha256!,
      takenAt: ready.takenAt!,
    });
    const open = vi.spyOn(browser, 'open').mockImplementation(() => {});
    fireEvent.click(screen.getByRole('button', { name: 'Take snapshot' }));
    await waitFor(() =>
      expect(start).toHaveBeenCalledWith('app', 'mongo-resource', expect.any(String)),
    );
    expect(await screen.findByText(/Copying the database/)).toBeVisible();
    expect(await screen.findByText(ready.filename!, {}, { timeout: 4000 })).toBeVisible();
    expect(status).toHaveBeenCalledWith('app', 'mongo-resource', 'snapshot');
    expect(screen.getByText('3 MB')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    await waitFor(() =>
      expect(open).toHaveBeenCalledWith('https://s3.example.test/exports/file.zip?signature'),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    await waitFor(() => expect(link).toHaveBeenCalledTimes(2));
  });

  it('explains a failed snapshot and starts over with a new request', async () => {
    show('admin');
    fireEvent.click(await screen.findByRole('button', { name: 'Download a MongoDB snapshot' }));
    const start = vi.spyOn(appManagementApi, 'requestSnapshot').mockResolvedValue(preparing);
    vi.spyOn(appManagementApi, 'snapshot').mockResolvedValue({
      ...preparing,
      state: 'failed',
      error: 'This database is larger than the 2 GiB snapshot limit.',
    });
    fireEvent.click(screen.getByRole('button', { name: 'Take snapshot' }));
    expect(await screen.findByText(/2 GiB snapshot limit/)).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Take a new snapshot' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Take snapshot' }));
    await waitFor(() => expect(start).toHaveBeenCalledTimes(2));
    expect(start.mock.calls[0][2]).not.toEqual(start.mock.calls[1][2]);
  });
});
