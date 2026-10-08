import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { api, type Intent } from '../api';
import { Overview } from '../pages/Overview';
import { HistoryPage } from '../pages/History';
import { AttentionActivity } from './AttentionActivity';

const blocked: Intent = {
  intentId: 'blocked',
  appId: 'app',
  appSlug: 'student-app',
  kind: 'deploy',
  state: 'blocked',
  commit: 'a'.repeat(40),
  createdAt: '2026-01-01T00:00:00Z',
  operationId: 'deploy',
  operation: { status: 'recovery_required', phase: 'startup_interrupted', cleanupState: 'pending' },
  safeError: 'This deploy hasn’t finished. Resume it from Activity.',
  actor: { displayName: 'Teammate', you: false },
  canResume: true,
};
function show(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
  return client;
}
describe('app activity that needs attention', () => {
  it.each([Overview, HistoryPage])('keeps an older blocked deploy visible on %s', async (Page) => {
    vi.spyOn(api, 'app').mockResolvedValue({
      applicationId: 'app',
      slug: 'student-app',
      savedRevision: 1,
      lifecycleState: 'ready',
      acceptedDeployment: null,
    } as never);
    vi.spyOn(api, 'history').mockResolvedValue({ items: [], nextCursor: null, truncated: false });
    vi.spyOn(api, 'activity').mockResolvedValue([]);
    vi.spyOn(api, 'attention').mockResolvedValue([blocked]);
    const resume = vi.spyOn(api, 'resume').mockResolvedValue({ ...blocked, state: 'accepted' });
    show(<Page id="app" />);
    expect(await screen.findByText('Teammate')).toBeVisible();
    expect(screen.getByText('Needs attention', { exact: true })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    await waitFor(() => expect(resume).toHaveBeenCalledWith('blocked'));
  });
  it('shows resume failures and keeps the blocked change available', async () => {
    vi.spyOn(api, 'attention').mockResolvedValue([blocked]);
    vi.spyOn(api, 'resume').mockRejectedValue(new Error('Try again in a minute.'));
    show(<AttentionActivity id="app" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Resume' }));
    expect(await screen.findByText('Try again in a minute.')).toBeVisible();
    expect(screen.getByText('Needs attention', { exact: true })).toBeVisible();
  });
  it('does not offer generic resume for another actor’s environment edit', async () => {
    vi.spyOn(api, 'attention').mockResolvedValue([
      { ...blocked, kind: 'env_set', canResume: false, requiresResubmit: false },
    ]);
    show(<AttentionActivity id="app" />);
    expect(await screen.findByText(/person who started this environment edit/)).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull();
  });
});
