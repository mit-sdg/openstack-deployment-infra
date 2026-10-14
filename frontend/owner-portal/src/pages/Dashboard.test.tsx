import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, type AppRecord } from '../api';
import { AppStatus } from '../components/AppFrame';
import { appRefetchInterval } from '../utils/presentation';
import { Dashboard, appsRefetchInterval } from './Dashboard';

const app: AppRecord = {
  applicationId: 'app',
  slug: 'demo',
  url: null,
  savedRevision: 0,
  lifecycleState: 'ready',
  desiredRunning: true,
  activeDeploymentId: 'deployment',
  acceptedDeployment: {
    deploymentId: 'deployment',
    sourceCommit: 'a'.repeat(40),
    acceptedAt: '2026-10-14T10:00:00Z',
  },
  health: { allocationHealthy: true, routeHealthy: true, schedulerState: 'running' },
  stale: true,
  observedAt: '2026-10-14T10:00:00Z',
  observationAgeSeconds: 40,
  refreshing: true,
};
const quota = {
  apps: { limit: 2, used: 1, reserved: 0 },
  concurrentOperations: { limit: 1, used: 0, reserved: 0 },
};
function show(items = [app]) {
  vi.spyOn(api, 'apps').mockResolvedValue({ items, quota, nextCursor: null, truncated: false });
  vi.spyOn(api, 'intents').mockResolvedValue({ items: [], nextCursor: null, truncated: false });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <Dashboard />
    </QueryClientProvider>,
  );
  return client;
}
afterEach(() => {
  vi.useRealTimers();
});
describe('My apps cached observations', () => {
  it('shows Checking for stale health while refresh is pending', async () => {
    show();
    expect(await screen.findByText('Checking')).toBeVisible();
    expect(screen.queryByText('Unknown')).toBeNull();
    expect(screen.getByRole('link', { name: 'demo' })).toBeVisible();
  });
  it('shows Checking for a missing observation instead of Not deployed', async () => {
    show([
      {
        ...app,
        observedAt: null,
        observationAgeSeconds: null,
        health: null,
        acceptedDeployment: null,
      },
    ]);
    expect(await screen.findByText('Checking')).toBeVisible();
    expect(screen.queryByText('Unknown')).toBeNull();
  });
  it('never shows bare Unknown while a row with unknown health is refreshing', async () => {
    show([{ ...app, stale: false, health: null }]);
    expect(await screen.findByText('Checking')).toBeVisible();
    expect(screen.queryByText('Unknown')).toBeNull();
  });
  it('uses the same Checking state in the app header', () => {
    render(<AppStatus app={{ ...app, observationAgeSeconds: 10 }} />);
    expect(screen.getByText('Checking')).toBeVisible();
  });
  it('polls quickly while refreshing and returns to the regular interval', () => {
    expect(appsRefetchInterval([app])).toBe(1500);
    expect(appsRefetchInterval([{ ...app, refreshing: false }])).toBe(5000);
    expect(appsRefetchInterval([])).toBe(5000);
    expect(appsRefetchInterval(undefined)).toBe(5000);
    expect(appRefetchInterval(app)).toBe(1500);
    expect(appRefetchInterval({ ...app, refreshing: false })).toBe(5000);
  });
  it('refetches a pending row promptly and settles on its refreshed health', async () => {
    vi.useFakeTimers();
    const client = show();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText('Checking')).toBeVisible();
    vi.mocked(api.apps).mockResolvedValue({
      items: [{ ...app, stale: false, refreshing: false, observationAgeSeconds: 0 }],
      quota,
      nextCursor: null,
      truncated: false,
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(api.apps).toHaveBeenCalledTimes(2);
    expect(screen.getByText('Healthy')).toBeVisible();
    expect(screen.queryByText('Checking')).toBeNull();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });
    expect(api.apps).toHaveBeenCalledTimes(2);
    client.clear();
  });
});
