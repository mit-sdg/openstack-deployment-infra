import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { ApiError, type TeamMember } from '../api';
import { TeamSection } from './TeamSection';

const owner: TeamMember = {
  userId: 'alice',
  username: 'alice',
  displayName: 'Alice Student',
  method: 'provider',
  role: 'owner',
  addedAt: null,
};
const bob: TeamMember = {
  userId: 'bob',
  username: 'bob',
  displayName: 'Bob Student',
  method: 'provider',
  role: 'member',
  addedAt: '2026-10-04T00:00:00Z',
};
function wrap(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{children}</QueryClientProvider>);
}

describe('app team', () => {
  it('lets the owner add and remove teammates', async () => {
    const service = {
      members: vi.fn(() => Promise.resolve({ items: [owner], you: 'alice', access: 'owner' })),
      addMember: vi.fn((_id: string, username: string) =>
        username === 'bob'
          ? Promise.resolve({ items: [owner, bob] })
          : Promise.reject(
              new ApiError(
                404,
                'ACCOUNT_NOT_REGISTERED',
                "nobody isn't registered yet. Ask them to sign in to the portal once, then add them.",
              ),
            ),
      ),
      removeMember: vi.fn(() => Promise.resolve({ items: [owner], left: false })),
    };
    wrap(<TeamSection id="app" service={service} />);
    const team = await screen.findByRole('table', { name: 'Team' });
    expect(within(team).getByText('Alice Student')).toBeVisible();
    expect(within(team).getByText('(you)')).toBeVisible();
    fireEvent.change(screen.getByLabelText('Add by username'), { target: { value: 'nobody' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add to team' }));
    expect(
      await screen.findByText(
        "nobody isn't registered yet. Ask them to sign in to the portal once, then add them.",
      ),
    ).toBeVisible();
    fireEvent.change(screen.getByLabelText('Add by username'), { target: { value: ' bob ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add to team' }));
    expect(await within(team).findByText('Bob Student')).toBeVisible();
    expect(service.addMember).toHaveBeenLastCalledWith('app', 'bob');
    expect(screen.getByLabelText('Add by username')).toHaveValue('');
    fireEvent.click(within(team).getByRole('button', { name: 'Remove' }));
    const dialog = screen.getByRole('dialog', { name: 'Remove Bob Student?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }));
    await waitFor(() => expect(service.removeMember).toHaveBeenCalledWith('app', 'bob'));
    await waitFor(() => expect(within(team).queryByText('Bob Student')).toBeNull());
  });

  it('lets a member leave but not change the team', async () => {
    const service = {
      members: vi.fn(() => Promise.resolve({ items: [owner, bob], you: 'bob', access: 'member' })),
      addMember: vi.fn(),
      removeMember: vi.fn(() => Promise.resolve({ items: [], left: true })),
    };
    wrap(<TeamSection id="app" service={service} />);
    const team = await screen.findByRole('table', { name: 'Team' });
    expect(screen.queryByLabelText('Add by username')).toBeNull();
    expect(screen.getByText(/Only the owner adds or removes people/)).toBeVisible();
    fireEvent.click(within(team).getByRole('button', { name: 'Leave' }));
    const dialog = screen.getByRole('dialog', { name: 'Leave this app?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Leave app' }));
    await waitFor(() => expect(service.removeMember).toHaveBeenCalledWith('app', 'bob'));
  });
});
