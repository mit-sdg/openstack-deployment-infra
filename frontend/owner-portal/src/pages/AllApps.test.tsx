import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';
import type { CatalogApp } from '../appManagementApi';
import { CatalogStatus, catalogRefetchInterval } from './AllApps';

const app: CatalogApp = {
  applicationId: 'app',
  slug: 'demo',
  ownerId: 'owner',
  ownerUsername: 'alice',
  ownerDisplayName: 'Alice',
  lifecycleState: 'ready',
  savedRevision: 1,
  appState: 'healthy',
  observedAt: '2026-10-08T12:00:00Z',
  refreshing: false,
  attention: [],
  url: null,
  lastDeployedAt: null,
};
function show(node: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}
describe('catalog status', () => {
  it('shows only the app state and a check-time tooltip when attention is empty', () => {
    show(<CatalogStatus app={app} />);
    expect(screen.getByText('Healthy')).toBeVisible();
    expect(screen.getByTitle(/^Last checked /)).toBeVisible();
    expect(screen.queryByText(/attention/i)).toBeNull();
    expect(screen.queryByText('—')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
  });
  it('says Checking, not Unknown, while a stale row is being re-read', () => {
    show(
      <CatalogStatus app={{ ...app, appState: 'unknown', refreshing: true, observedAt: null }} />,
    );
    expect(screen.getByText('Checking')).toBeVisible();
    expect(screen.queryByText('Unknown')).toBeNull();
  });
  it('still says Unknown when nothing is re-reading the row', () => {
    show(
      <CatalogStatus app={{ ...app, appState: 'unknown', refreshing: false, observedAt: null }} />,
    );
    expect(screen.getByText('Unknown')).toBeVisible();
  });
  it('shows compact activity statuses and their available actions below the state', () => {
    show(
      <CatalogStatus
        app={{
          ...app,
          attention: [
            {
              intentId: 'deployment',
              appId: 'app',
              appSlug: 'demo',
              kind: 'deploy',
              state: 'blocked',
              operationId: null,
              operation: null,
              safeError: null,
              commit: null,
              createdAt: '2026-10-08T12:00:00Z',
              canResume: true,
            },
          ],
        }}
      />,
    );
    expect(screen.getByText('Healthy')).toBeVisible();
    expect(screen.getByText('Needs attention')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Resume' })).toBeVisible();
  });
  it('polls faster while unknown or refreshing and returns to the regular interval', () => {
    expect(catalogRefetchInterval([{ ...app, appState: 'unknown' }])).toBe(3000);
    expect(catalogRefetchInterval([{ ...app, refreshing: true }])).toBe(3000);
    expect(catalogRefetchInterval([app])).toBe(15000);
    expect(catalogRefetchInterval([])).toBe(15000);
  });
});
