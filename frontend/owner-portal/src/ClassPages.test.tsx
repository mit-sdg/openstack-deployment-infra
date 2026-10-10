import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '@openstack-platform/ui';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import { api, ApiError, clearCredentials, type Session, type Intent } from './api';
import { adminApi } from './adminApi';
import { appManagementApi } from './appManagementApi';
import { classApi } from './classApi';
import { PeoplePage, PersonPage } from './pages/People';
import { AllAppsPage } from './pages/AllApps';
import { ActivityPage } from './pages/Activity';
import { useActive } from './hooks/useClassReads';
import { CreateAccountAction } from './components/AccountControls';
const id = '11111111-1111-4111-8111-111111111111';
const person = {
  ownerId: id,
  username: 'alice',
  displayName: '<script>Alice</script>',
  role: 'owner' as const,
  portalEnabled: true,
  status: 'active' as const,
};
const empty = { items: [], nextCursor: null, truncated: false };
let session: Session;
function mockSession(stepUpExpiresAt: string | null) {
  session = {
    role: 'admin',
    csrfToken: 'csrf',
    platformName: 'Example platform',
    stepUpExpiresAt,
    user: { id, username: 'admin', displayName: 'Admin' },
    expiresAt: new Date(Date.now() + 3600000).toISOString(),
    quota: {
      apps: { limit: null, used: 0, reserved: 0 },
      concurrentOperations: { limit: null, used: 0, reserved: 0 },
    },
  };
  vi.spyOn(api, 'session').mockResolvedValue(session);
}
function show(node: ReactNode, path = '/people') {
  window.history.replaceState(null, '', path);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  if (session) {
    client.setQueryDefaults(['session'], { gcTime: Infinity });
    client.setQueryData(['session'], session);
  }
  render(
    <QueryClientProvider client={client}>
      <ToastProvider>{node}</ToastProvider>
    </QueryClientProvider>,
  );
  return client;
}
describe('class pages', () => {
  it.each([false, true])(
    'lists everyone and gates account creation for admin=%s',
    async (admin) => {
      vi.spyOn(classApi, 'people').mockResolvedValue({
        ...empty,
        items: [
          person,
          { ...person, ownerId: '22222222-2222-4222-8222-222222222222', role: 'staff' },
        ],
      });
      const account = vi.spyOn(adminApi, 'account');
      show(<PeoplePage admin={admin} />);
      expect(await screen.findAllByText(person.displayName)).toHaveLength(2);
      expect(document.querySelector('script')).toBeNull();
      expect(!!screen.queryByRole('button', { name: 'Create account' })).toBe(admin);
      expect(account).not.toHaveBeenCalled();
    },
  );
  it('shows blocked apps and resumes them using the shared action', async () => {
    const intent: Intent = {
      intentId: id,
      appId: id,
      appSlug: 'app',
      kind: 'deploy',
      state: 'unknown',
      createdAt: '',
      commit: null,
      operationId: id,
      operation: null,
      safeError: null,
      canResume: true,
    };
    vi.spyOn(appManagementApi, 'list').mockResolvedValue({
      ...empty,
      items: [
        {
          applicationId: id,
          slug: 'app',
          ownerId: id,
          ownerUsername: 'alice',
          ownerDisplayName: 'Alice',
          savedRevision: 1,
          lifecycleState: 'ready',
          appState: 'not_deployed',
          observedAt: null,
          refreshing: false,
          attention: [intent],
          url: null,
          lastDeployedAt: null,
        },
      ],
    });
    const resume = vi.spyOn(api, 'resume').mockResolvedValue({ ...intent, state: 'accepted' });
    show(<AllAppsPage />, '/all-apps');
    expect(await screen.findByText('Unknown')).toBeVisible();
    fireEvent.click(await screen.findByRole('button', { name: 'Resume' }));
    await waitFor(() => expect(resume).toHaveBeenCalledWith(id));
  });
  it('puts older blocked activity at the top and offers Resume', async () => {
    const row = {
      intentId: id,
      applicationId: id,
      applicationSlug: 'app',
      ownerId: id,
      ownerUsername: 'alice',
      ownerDisplayName: 'Alice',
      kind: 'deploy',
      state: 'blocked',
      stage: 'recovery',
      cleanupState: 'pending',
      createdAt: '',
      updatedAt: null,
      statusObservedAt: null,
      attention: 'recovery_required',
      controllerErrorCode: null,
      guidance: null,
      operationId: id,
      canResume: true,
      requiresResubmit: false,
    };
    vi.spyOn(classApi, 'activity').mockImplementation(async (_o, _a, _c, _s, attention) => ({
      ...empty,
      items: attention ? [row] : [{ ...row, intentId: 'other', state: 'succeeded' }],
    }));
    show(<ActivityPage />, '/activity');
    expect(await screen.findByRole('button', { name: 'Resume' })).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Needs attention' })).toBeVisible();
  });
  it('accepts storage limits activity from the broker envelope', async () => {
    const row = {
      intentId: id,
      applicationId: id,
      applicationSlug: 'app',
      ownerId: id,
      ownerUsername: 'alice',
      ownerDisplayName: 'Alice',
      kind: 'storage_limits',
      state: 'succeeded',
      stage: 'settled',
      cleanupState: 'not_required',
      createdAt: null,
      updatedAt: null,
      statusObservedAt: null,
      attention: 'none',
      controllerErrorCode: null,
      guidance: null,
      operationId: id,
      canResume: false,
      requiresResubmit: false,
    };
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ data: { ...empty, items: [row] } }),
      }),
    );
    const response = await classApi.activity();
    expect(response.items[0]).toEqual(row);
  });
  it('rejects unknown roles and expanded metadata', async () => {
    for (const item of [
      { ...person, role: 'root' },
      { ...person, role: ['owner'] },
      { ...person, refs: 'SECRET' },
    ]) {
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue({
          ok: true,
          status: 200,
          json: async () => ({ data: { ...empty, items: [item] } }),
        }),
      );
      await expect(classApi.people()).rejects.toThrow('Invalid class metadata');
    }
  });
  it('drops late class metadata after sign-out', async () => {
    let resolve!: (value: unknown) => void;
    vi.stubGlobal(
      'fetch',
      vi.fn().mockReturnValue(
        new Promise((done) => {
          resolve = done;
        }),
      ),
    );
    const pending = classApi.people();
    clearCredentials();
    resolve({ ok: true, status: 200, json: async () => ({ data: { ...empty, items: [person] } }) });
    await expect(pending).rejects.toThrow('Sign in to continue');
  });
  it('asks for the password and code when a sensitive action needs them, then continues', async () => {
    mockSession(null);
    const reauthenticate = vi
      .spyOn(adminApi, 'reauthenticate')
      .mockResolvedValue({ stepUpExpiresAt: new Date(Date.now() + 300000).toISOString() });
    const create = vi
      .spyOn(adminApi, 'create')
      .mockResolvedValue({ userId: id, setupUrl: 'https://portal.test/setup#TOKEN' });
    show(<CreateAccountAction />, '/people');
    fireEvent.click(await screen.findByRole('button', { name: 'Create account' }));
    const dialog = screen.getByRole('dialog', { name: 'Create account' });
    fireEvent.change(within(dialog).getByLabelText('Username'), { target: { value: 'newstaff' } });
    fireEvent.change(within(dialog).getByLabelText('Role'), { target: { value: 'staff' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create account' }));
    const stepUp = await screen.findByRole('dialog', { name: 'Confirm it’s you' });
    expect(create).not.toHaveBeenCalled();
    fireEvent.change(within(stepUp).getByLabelText('Password'), { target: { value: 'pw' } });
    fireEvent.change(within(stepUp).getByLabelText('Authentication code'), {
      target: { value: '123456' },
    });
    fireEvent.click(within(stepUp).getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(create).toHaveBeenCalledWith('newstaff', 'newstaff', 'staff'));
    expect(reauthenticate).toHaveBeenCalledWith('pw', '123456');
    expect(await screen.findByLabelText('Setup link')).toHaveValue(
      'https://portal.test/setup#TOKEN',
    );
  });
  it('retries after the server asks for step-up, and cancelling shows no error', async () => {
    mockSession(new Date(Date.now() + 300000).toISOString());
    const create = vi
      .spyOn(adminApi, 'create')
      .mockRejectedValue(new ApiError(403, 'STEP_UP_REQUIRED', 'Re-enter your password.'));
    show(<CreateAccountAction />, '/people');
    fireEvent.click(await screen.findByRole('button', { name: 'Create account' }));
    const dialog = screen.getByRole('dialog', { name: 'Create account' });
    fireEvent.change(within(dialog).getByLabelText('Username'), { target: { value: 'x' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create account' }));
    const stepUp = await screen.findByRole('dialog', { name: 'Confirm it’s you' });
    expect(create).toHaveBeenCalledOnce();
    fireEvent.click(within(stepUp).getByRole('button', { name: 'Cancel' }));
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Confirm it’s you' })).not.toBeInTheDocument(),
    );
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
  });
});

function Probe({ role }: { role: Session['role'] }) {
  const active = useActive(role);
  return <span>{active ? 'Visible' : 'Hidden'}</span>;
}
afterEach(() => vi.useRealTimers());
describe('class read visibility and session lifetimes', () => {
  it.each([
    ['owner', 10, false],
    ['staff', 10, true],
    ['admin', 10, false],
    ['admin', 15, true],
  ] as const)('keeps the %s idle boundary at %s minutes', (role, minutes, expires) => {
    vi.useFakeTimers();
    const ended = vi.fn();
    window.addEventListener('portal-session-ended', ended);
    const view = render(<Probe role={role} />);
    act(() => vi.advanceTimersByTime(minutes * 60000));
    expect(ended).toHaveBeenCalledTimes(expires ? 1 : 0);
    view.unmount();
    window.removeEventListener('portal-session-ended', ended);
  });
  it('pauses class reads when the browser hides the page', () => {
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
    const view = render(<Probe role="staff" />);
    expect(view.getByText('Visible')).toBeVisible();
    hidden.mockReturnValue(true);
    act(() => document.dispatchEvent(new Event('visibilitychange')));
    expect(view.getByText('Hidden')).toBeVisible();
  });
});
