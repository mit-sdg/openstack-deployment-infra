import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { api, ApiError, type AppRecord } from '../api';
import { AppFrame } from './AppFrame';
import { Loaded } from './ClassRecords';
import { QueryError } from './Feedback';
import { useRead } from '../hooks/useClassReads';

const app: AppRecord = {
  applicationId: 'app',
  slug: 'staff-review',
  savedRevision: 1,
  lifecycleState: 'ready',
  access: 'admin',
  ownerId: 'owner',
  desiredRunning: false,
  activeDeploymentId: null,
  acceptedDeployment: null,
  health: null,
  stale: false,
  observedAt: null,
  url: null,
};

describe('loaded app pages', () => {
  it('keeps the loaded app, tabs and content when a background refetch fails', async () => {
    const read = vi.spyOn(api, 'app').mockResolvedValue(app);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    render(
      <QueryClientProvider client={client}>
        <AppFrame id="app" active="Overview">
          Loaded details
        </AppFrame>
      </QueryClientProvider>,
    );
    expect(await screen.findByRole('heading', { name: 'staff-review' })).toBeVisible();
    read.mockRejectedValue(new ApiError(503, 'STATE_UNAVAILABLE', 'Unavailable.'));
    await act(async () => {
      await client.refetchQueries({ queryKey: ['app', 'app'] });
    });
    expect(client.getQueryState(['app', 'app'])?.status).toBe('error');
    expect(screen.getByRole('heading', { name: 'staff-review' })).toBeVisible();
    expect(screen.getByRole('navigation', { name: 'App pages' })).toBeVisible();
    expect(screen.getByText('Loaded details')).toBeVisible();
    expect(screen.queryByText(/Couldn't load this app/)).toBeNull();
  });

  it('shows the app error when no app data has loaded', async () => {
    vi.spyOn(api, 'app').mockRejectedValue(new ApiError(503, 'STATE_UNAVAILABLE', 'Unavailable.'));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    render(
      <QueryClientProvider client={client}>
        <AppFrame id="app" active="Overview">
          Details
        </AppFrame>
      </QueryClientProvider>,
    );
    expect(await screen.findByText("Couldn't load this app. Try again in a minute.")).toBeVisible();
    expect(screen.queryByText('Details')).toBeNull();
  });

  it('keeps class metadata when its refetch fails', async () => {
    const read = vi.fn().mockResolvedValue({ name: 'Loaded metadata' });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    function Metadata() {
      const query = useRead<{ name: string }>(['metadata'], read);
      return (
        <Loaded query={query} what="metadata">
          {(data) => data.name}
        </Loaded>
      );
    }
    render(
      <QueryClientProvider client={client}>
        <Metadata />
      </QueryClientProvider>,
    );
    expect(await screen.findByText('Loaded metadata')).toBeVisible();
    read.mockRejectedValue(new ApiError(503, 'STATE_UNAVAILABLE', 'Unavailable.'));
    await act(async () => {
      await client.refetchQueries({ queryKey: ['class'] });
    });
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    expect(screen.getByText('Loaded metadata')).toBeVisible();
    expect(screen.queryByText(/Couldn't load metadata/)).toBeNull();
  });

  it('suppresses section error banners when data is present, including empty results', () => {
    render(
      <QueryError
        query={{
          data: [],
          error: new ApiError(503, 'UNAVAILABLE', 'Unavailable.'),
          refetch: vi.fn(),
          isFetching: false,
        }}
        what="deployments"
      />,
    );
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
