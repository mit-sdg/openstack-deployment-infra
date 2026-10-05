import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';

const options = { data: { csrfToken: 'anon', providerLabel: 'class account' } };
const clients: QueryClient[] = [];
function show(path: string) {
  window.history.replaceState(null, '', path);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  clients.push(client);
  render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  );
}
function respond(...bodies: { ok: boolean; body: unknown }[]) {
  const fetch = vi.fn();
  for (const { ok, body } of bodies)
    fetch.mockResolvedValueOnce({ ok, status: ok ? 200 : 401, json: async () => body });
  vi.stubGlobal('fetch', fetch);
  return fetch;
}
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  vi.unstubAllGlobals();
});

describe('sign-in page', () => {
  it('leads with a plain link to class sign-in and keeps local accounts behind a disclosure', async () => {
    respond({ ok: true, body: options });
    show('/sign-in');
    const link = await screen.findByRole('link', { name: 'Sign in with your class account' });
    // A full navigation, not client-side routing: the server sends the browser on.
    expect(link).toHaveAttribute('href', '/auth/commons/start');
    expect(link).toHaveClass('ui-button--primary');
    expect(screen.getByText('You’ll confirm with your class account and come right back.'));
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument();
    const disclosure = screen.getByRole('button', { name: 'Use a local account' });
    expect(disclosure).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(disclosure);
    expect(disclosure).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByLabelText('Username')).toHaveFocus();
    expect(screen.getByLabelText('Password')).toBeVisible();
    expect(screen.getByLabelText('Authentication code')).toBeVisible();
  });

  it('signs in local accounts with the local method only', async () => {
    const fetch = respond(
      { ok: true, body: options },
      { ok: false, body: { error: { code: 'INVALID_CREDENTIALS' } } },
    );
    show('/sign-in');
    fireEvent.click(await screen.findByRole('button', { name: 'Use a local account' }));
    fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'admin' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'pw' } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sign in' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Username, password or authentication code is incorrect.',
    );
    const [url, init] = fetch.mock.calls[1];
    expect(url).toBe('/auth/login');
    expect(JSON.parse(init.body)).toEqual({
      csrfToken: 'anon',
      username: 'admin',
      password: 'pw',
      method: 'local',
      totp: '',
    });
  });

  for (const [code, text] of [
    ['COMMONS_CANCELLED', 'Sign-in was cancelled.'],
    ['SIGN_IN_EXPIRED', 'That sign-in expired. Try again.'],
    [
      'IDENTITY_UNAVAILABLE',
      'Signing in with your class account is unavailable right now. Try again in a minute.',
    ],
    ['ACCOUNT_DISABLED', 'This account is disabled or archived.'],
    ['RATE_LIMITED', 'Too many attempts. Wait a minute, then try again.'],
    ['SIGN_IN_UNAVAILABLE', 'Sign-in is unavailable right now.'],
    ['SIGN_IN_FAILED', 'Sign-in didn’t work. Try again.'],
    ['constructor', 'Sign-in didn’t work. Try again.'],
    ['<b>RATE_LIMITED</b>', 'Sign-in didn’t work. Try again.'],
  ] as const) {
    it(`explains a returned ${code} once and clears it from the address`, async () => {
      respond({ ok: true, body: options });
      show(`/sign-in?error=${encodeURIComponent(code)}`);
      expect(await screen.findByRole('alert')).toHaveTextContent(text);
      await waitFor(() => expect(window.location.search).toBe(''));
      expect(window.location.pathname).toBe('/sign-in');
      expect(screen.getByRole('alert')).toHaveTextContent(text);
    });
  }
});
