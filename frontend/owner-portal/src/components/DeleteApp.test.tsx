import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { api, ApiError, type AppRecord, type Intent, type Session } from '../api';
import { appManagementApi } from '../appManagementApi';
import { adminApi } from '../adminApi';
import { DeleteApp } from './DeleteApp';

const app = {
  applicationId: 'app',
  slug: 'demo',
  access: 'admin',
  identityProvider: false,
} as AppRecord;
const operation: Intent = {
  intentId: 'delete-intent',
  appId: 'app',
  appSlug: 'demo',
  kind: 'app_delete',
  state: 'accepted',
  operationId: 'operation',
  operation: { status: 'running', phase: 'storage_removing', cleanupState: 'pending' },
  createdAt: new Date().toISOString(),
  safeError: null,
  commit: null,
};
function show(role: Session['role'], recent = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  client.setQueryData(['session'], {
    role,
    stepUpExpiresAt: recent ? new Date(Date.now() + 300000).toISOString() : null,
  });
  client.setQueryData(['environment', 'app'], { items: [{ name: 'SECRET' }] });
  render(
    <QueryClientProvider client={client}>
      <DeleteApp app={app} />
    </QueryClientProvider>,
  );
  return client;
}
async function confirm() {
  fireEvent.click(await screen.findByRole('button', { name: 'Delete app' }));
  fireEvent.change(screen.getByLabelText('Type “demo” to confirm'), { target: { value: 'demo' } });
  fireEvent.click(screen.getByRole('button', { name: 'Delete permanently' }));
}

describe('Delete app', () => {
  it.each(['owner', 'staff'] as const)(
    'hides the action for %s even with app administration access',
    (role) => {
      show(role);
      expect(screen.queryByRole('button', { name: 'Delete app' })).toBeNull();
    },
  );
  it('requires the exact slug, explains the purge, and shows progress and success', async () => {
    const remove = vi.spyOn(appManagementApi, 'deleteApp').mockResolvedValue(operation);
    const poll = vi.spyOn(api, 'intent').mockResolvedValue(operation);
    const client = show('admin');
    fireEvent.click(await screen.findByRole('button', { name: 'Delete app' }));
    expect(screen.getByText(/running app and its worker/)).toHaveTextContent(
      /every database.*S3 bucket.*environment variables.*deploy key.*all members/,
    );
    expect(screen.getByText(/Last successful platform backup: unavailable/)).toBeVisible();
    const button = screen.getByRole('button', { name: 'Delete permanently' });
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Type “demo” to confirm'), {
      target: { value: 'demo ' },
    });
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Type “demo” to confirm'), {
      target: { value: 'demo' },
    });
    fireEvent.click(button);
    await screen.findByText('Deleting app');
    expect(remove).toHaveBeenCalledWith('app', 'demo', expect.any(String));
    poll.mockResolvedValue({ ...operation, state: 'succeeded' });
    await client.invalidateQueries({ queryKey: ['intent', 'delete-intent'] });
    await screen.findByText('The app and its data have been deleted.');
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    await waitFor(() => expect(client.getQueryData(['environment', 'app'])).toBeUndefined());
    expect(window.location.pathname).toBe('/all-apps');
  });
  it('requires step-up before dispatch and retains the key after a transport failure', async () => {
    const remove = vi
      .spyOn(appManagementApi, 'deleteApp')
      .mockRejectedValueOnce(new Error('Network unavailable'))
      .mockResolvedValue(operation);
    vi.spyOn(api, 'intent').mockResolvedValue(operation);
    const reauthenticate = vi
      .spyOn(adminApi, 'reauthenticate')
      .mockResolvedValue({ stepUpExpiresAt: new Date(Date.now() + 300000).toISOString() });
    vi.spyOn(api, 'session').mockReturnValue(new Promise(() => {}));
    show('admin', false);
    await confirm();
    expect(await screen.findByRole('dialog', { name: 'Confirm it’s you' })).toBeVisible();
    expect(remove).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'password' } });
    fireEvent.change(screen.getByLabelText('Authentication code'), { target: { value: '123456' } });
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(remove).toHaveBeenCalledTimes(1));
    expect(reauthenticate).toHaveBeenCalledWith('password', '123456');
    await screen.findByRole('button', { name: 'Delete permanently' });
    fireEvent.click(screen.getByRole('button', { name: 'Delete permanently' }));
    await screen.findByText('Deleting app');
    expect(remove.mock.calls[0][2]).toBe(remove.mock.calls[1][2]);
  });
  it('shows blocked deletion with an admin resume that also requires step-up', async () => {
    const blocked = {
      ...operation,
      state: 'blocked',
      canResume: true,
      safeError: 'Resume this change to finish it.',
    };
    vi.spyOn(appManagementApi, 'deleteApp').mockResolvedValue(blocked);
    vi.spyOn(api, 'intent').mockResolvedValue(blocked);
    const resume = vi
      .spyOn(api, 'resume')
      .mockRejectedValue(new ApiError(403, 'STEP_UP_REQUIRED', 'Confirm it’s you.'));
    show('admin');
    await confirm();
    fireEvent.click(await screen.findByRole('button', { name: 'Resume' }));
    expect(await screen.findByRole('dialog', { name: 'Confirm it’s you' })).toBeVisible();
    expect(resume).toHaveBeenCalledWith('delete-intent');
  });
});
