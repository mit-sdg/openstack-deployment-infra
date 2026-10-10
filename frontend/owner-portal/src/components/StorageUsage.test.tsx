import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { api, type StorageResource } from '../api';
import { StorageSection } from './StorageSection';
import { StorageUsage } from './StorageUsage';

const resource: StorageResource = {
  resourceId: 'resource',
  type: 'mongo',
  label: 'MongoDB',
  status: 'ready',
  createdAt: '2026-10-10T12:00:00Z',
  verifiedAt: null,
  isolation: 'instance',
  hardQuotaBytes: 2684354560,
  defaultBindings: {},
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
};
describe('storage usage and limits', () => {
  it('keeps measured values truthful and marks near-limit, full and blocked usage', () => {
    const view = render(<StorageUsage resource={resource} />);
    expect(screen.getByText('Usage not measured yet')).toBeVisible();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    for (const [ratio, tone] of [
      [0.79, 'neutral'],
      [0.8, 'warning'],
      [0.95, 'warning'],
      [1, 'critical'],
    ] as const) {
      view.rerender(
        <StorageUsage
          resource={{
            ...resource,
            quotas: { ...resource.quotas, connections: 100 },
            usage: {
              ...resource.usage,
              usedBytes: Math.ceil(resource.quotas.sizeBytes! * ratio),
              currentConnections: Math.round(100 * ratio),
              instanceMemoryBytes: Math.ceil(resource.quotas.memoryBytes! * ratio),
              measuredAt: '2026-10-10T12:00:00Z',
            },
          }}
        />,
      );
      for (const metric of ['size', 'connections', 'memory'])
        expect(
          screen.getByRole('progressbar', { name: `MongoDB ${metric} usage` }),
        ).toHaveAttribute('data-tone', tone);
    }
    view.rerender(
      <StorageUsage
        resource={{
          ...resource,
          usage: { ...resource.usage, usedBytes: 3221225472, measuredAt: '2026-10-10T12:00:00Z' },
          writeBlock: {
            blocked: true,
            reason: 'size_limit_exceeded',
            since: '2026-10-10T12:00:00Z',
          },
        }}
      />,
    );
    expect(screen.getByRole('progressbar')).toHaveAttribute('max', '2147483648');
    expect(screen.getByRole('progressbar')).toHaveAttribute('value', '2147483648');
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuetext', '3 GB of 2 GB');
    expect(screen.getByRole('progressbar')).toHaveAttribute('data-tone', 'critical');
    expect(screen.getByText(/You can still read and delete/)).toBeVisible();
    expect(screen.getByText(/writes resume automatically/)).toBeVisible();
    view.rerender(
      <StorageUsage
        resource={{
          ...resource,
          usage: { ...resource.usage, usedBytes: 2040109466 },
          writeBlock: {
            blocked: true,
            reason: 'size_limit_exceeded',
            since: '2026-10-10T12:00:00Z',
          },
        }}
      />,
    );
    expect(screen.getByRole('progressbar')).toHaveAttribute('data-tone', 'critical');
  });
  it('exposes editing only to admins and submits complete quotas with the original expectation', async () => {
    vi.spyOn(api, 'storage').mockResolvedValue({ items: [resource], intents: [] });
    vi.spyOn(api, 'environment').mockResolvedValue({ revision: 1, items: [], updatedAt: null });
    vi.spyOn(api, 'app').mockResolvedValue({ access: 'admin' } as Awaited<
      ReturnType<typeof api.app>
    >);
    const intent = {
      intentId: 'intent',
      appId: 'app',
      appSlug: 'example',
      commit: null,
      createdAt: '2026-10-10T12:00:00Z',
      kind: 'storage_limits',
      state: 'accepted',
      operationId: 'operation',
      operation: null,
      safeError: null,
    };
    const save = vi.spyOn(api, 'setStorageLimits').mockResolvedValue(intent);
    vi.spyOn(api, 'intent').mockResolvedValue({ ...intent, state: 'succeeded' });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const content = (admin: boolean) => (
      <QueryClientProvider client={client}>
        <StorageSection id="app" bindings={[]} onChange={() => {}} admin={admin} />
      </QueryClientProvider>
    );
    const view = render(content(false));
    await screen.findByText('Usage not measured yet');
    expect(screen.queryByRole('button', { name: 'Edit MongoDB limits' })).not.toBeInTheDocument();
    view.rerender(content(true));
    const edit = screen.getByRole('button', { name: 'Edit MongoDB limits' });
    expect(edit).toHaveClass('ui-button--secondary');
    expect(edit.closest('.ui-list__trailing')).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Edit MongoDB limits' }));
    expect(screen.getByLabelText('Size limit')).toHaveValue(2);
    fireEvent.change(screen.getByLabelText('Size limit'), { target: { value: '0' } });
    fireEvent.submit(screen.getByRole('form', { name: 'MongoDB limits' }));
    expect(save).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Size limit')).toHaveAttribute('aria-invalid', 'true');
    fireEvent.change(screen.getByLabelText('Size limit'), { target: { value: '1.2' } });
    fireEvent.change(screen.getByLabelText('CPU limit (cores)'), { target: { value: '1.001' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save limits' }));
    await waitFor(() =>
      expect(save).toHaveBeenCalledWith(
        'app',
        'resource',
        { connections: 10, sizeBytes: 1288490189, memoryBytes: 536870912, cpuMillicores: 1001 },
        resource.quotas,
        expect.any(String),
      ),
    );
    expect(await screen.findByText('Limits saved.')).toBeVisible();
  });
});
