import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { ApiError, type SourceAccess, type SourceKey } from '../api';
import { RecentCommits } from './RecentCommits';
import { RepositoryAccess } from './RepositoryAccess';

const key: Extract<SourceKey, { present: true }> = {
  present: true,
  publicKey: 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixture',
  fingerprint: 'SHA256:fixture',
  createdAt: '2026-10-04T00:00:00Z',
};
function wrap(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{children}</QueryClientProvider>);
}
function service(initial: SourceKey, access: SourceAccess | Error) {
  return {
    sourceKey: vi.fn(() => Promise.resolve(initial)),
    createSourceKey: vi.fn((_id: string, replace = false) =>
      Promise.resolve({ ...key, publicKey: key.publicKey + (replace ? '2' : '') }),
    ),
    checkSourceKey: vi.fn(() =>
      access instanceof Error ? Promise.reject(access) : Promise.resolve(access),
    ),
  };
}

describe('private repository access', () => {
  it('creates a key, explains where it goes, and checks access', async () => {
    const api = service(
      { present: false },
      { keyPresent: true, reachable: true, head: 'a'.repeat(40), branch: 'main', problem: null },
    );
    wrap(<RepositoryAccess id="app" service={api as never} saved />);
    fireEvent.click(await screen.findByRole('button', { name: 'Create deploy key' }));
    expect(await screen.findByLabelText('Deploy key')).toHaveValue(key.publicKey);
    expect(screen.getByText(/Settings → Deploy keys → Add deploy key/)).toBeVisible();
    expect(api.createSourceKey).toHaveBeenCalledWith('app', false);
    fireEvent.click(screen.getByRole('button', { name: 'Check access' }));
    expect(await screen.findByText('GitHub accepts the key. main is at aaaaaaaaa.')).toBeVisible();
  });

  it('names what to fix, and replaces a key only after confirming', async () => {
    const api = service(key, {
      keyPresent: true,
      reachable: false,
      head: null,
      branch: 'main',
      problem: 'key-refused',
    });
    wrap(<RepositoryAccess id="app" service={api as never} saved />);
    fireEvent.click(await screen.findByRole('button', { name: 'Check access' }));
    expect(await screen.findByText(/GitHub refused the key/)).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Replace key' }));
    const dialog = screen.getByRole('dialog', { name: 'Replace the deploy key?' });
    expect(api.createSourceKey).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Replace key' }));
    await waitFor(() => expect(api.createSourceKey).toHaveBeenCalledWith('app', true));
    expect(await screen.findByLabelText('Deploy key')).toHaveValue(key.publicKey + '2');
  });

  it('waits for saved settings and hides where deploy keys are unavailable', async () => {
    const api = service(key, new Error('unused'));
    const { unmount } = wrap(<RepositoryAccess id="app" service={api as never} saved={false} />);
    expect(await screen.findByRole('button', { name: 'Check access' })).toBeDisabled();
    unmount();
    const older = {
      ...api,
      sourceKey: vi.fn(() =>
        Promise.reject(new ApiError(409, 'SOURCE_KEYS_UNAVAILABLE', 'Not available yet.')),
      ),
    };
    wrap(<RepositoryAccess id="app" service={older as never} saved />);
    await waitFor(() => expect(older.sourceKey).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByText('Private repository')).toBeNull());
  });

  it('offers the latest commit through the deploy key when GitHub hides the repository', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(new Response('{}', { status: 404 }))),
    );
    const onSelect = vi.fn();
    const latest = vi.fn(() =>
      Promise.resolve({
        keyPresent: true as const,
        reachable: true,
        head: 'b'.repeat(40),
        branch: 'main',
        problem: null,
      }),
    );
    wrap(
      <RecentCommits
        repository="https://github.com/ada/private"
        branch="main"
        value=""
        onSelect={onSelect}
        latest={latest}
      />,
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Use the latest commit on main' }));
    await waitFor(() =>
      expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ sha: 'b'.repeat(40) })),
    );
  });
});
