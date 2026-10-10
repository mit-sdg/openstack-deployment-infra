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
  defaultBindings: {},
  quotas: { measuredTargetBytes: 2147483648 },
  usage: {
    usedBytes: null,
    objectCount: null,
    currentConnections: null,
    measuredAt: null,
    stale: true,
  },
  writeBlock: { blocked: false, reason: null, since: null },
};
describe('storage usage and limits', () => {
  it('shows an unmeasured limit and explains how to restore paused Mongo writes', () => {
    const view = render(<StorageUsage resource={resource} />);
    expect(screen.getByText('Usage not measured yet')).toBeVisible();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
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
    expect(screen.getByText(/You can still read and delete/)).toBeVisible();
    expect(screen.getByText(/writes resume automatically/)).toBeVisible();
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
    fireEvent.click(screen.getByRole('button', { name: 'Edit MongoDB limits' }));
    expect(screen.getByLabelText('Size limit')).toHaveValue(2);
    fireEvent.change(screen.getByLabelText('Size limit'), { target: { value: '0' } });
    fireEvent.submit(screen.getByRole('form', { name: 'MongoDB limits' }));
    expect(save).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Size limit')).toHaveAttribute('aria-invalid', 'true');
    fireEvent.change(screen.getByLabelText('Size limit'), { target: { value: '1.2' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save limits' }));
    await waitFor(() =>
      expect(save).toHaveBeenCalledWith(
        'app',
        'resource',
        { measuredTargetBytes: 1288490189 },
        resource.quotas,
        expect.any(String),
      ),
    );
    expect(await screen.findByText('Limits saved.')).toBeVisible();
  });
});
