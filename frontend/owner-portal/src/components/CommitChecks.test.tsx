import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { Configuration } from '../api';
import { CommitChecks } from './CommitChecks';

const configuration: Configuration = {
  schemaVersion: 1,
  build: { runtime: 'node', packages: ['.'], buildScript: 'build', startScript: 'start' },
  runtime: { port: 3000, healthPath: '/health' },
  storageBindings: [],
};
function show(sha = 'a'.repeat(40)) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <CommitChecks
        repository="https://github.com/ada/app"
        sha={sha}
        configuration={configuration}
      />
    </QueryClientProvider>,
  );
}
function github(tree: unknown, packageJson = '{"scripts":{"start":"node ."}}', status = 200) {
  const fetcher = vi.fn((url: string) =>
    Promise.resolve(
      status !== 200
        ? new Response('{}', { status })
        : new Response(url.includes('/git/trees/') ? JSON.stringify(tree) : packageJson),
    ),
  );
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

describe('commit checks', () => {
  it('lists what to fix before deploying', async () => {
    github({
      tree: [{ path: 'package.json', type: 'blob', mode: '100644', size: 30 }],
    });
    show();
    expect(await screen.findByText('This commit will fail to build')).toBeVisible();
    expect(screen.getByText('package.json has no "build" script.')).toBeVisible();
    expect(screen.getByText('Commit package-lock.json in the repository root.')).toBeVisible();
  });

  it('uses private checkout checks with the saved revision after a browser failure', async () => {
    github({}, '', 404);
    const source = {
      sourceKey: vi.fn(() =>
        Promise.resolve({
          present: true as const,
          publicKey: 'ssh-ed25519 fixture',
          fingerprint: 'SHA256:fixture',
          createdAt: '',
        }),
      ),
      recentSourceCommits: vi.fn(),
      checkSourceCommit: vi.fn(() =>
        Promise.resolve([
          {
            id: 'script:build',
            label: 'Build script',
            state: 'problem' as const,
            problem: 'package.json has no "build" script.',
          },
        ]),
      ),
    };
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <CommitChecks
          repository="https://github.com/ada/private"
          sha={'c'.repeat(40)}
          configuration={configuration}
          platform={{ id: 'app', revision: 7, service: source }}
        />
      </QueryClientProvider>,
    );
    expect(await screen.findByText('package.json has no "build" script.')).toBeVisible();
    expect(source.checkSourceCommit).toHaveBeenCalledWith('app', 'c'.repeat(40), 7);
  });

  it('says when GitHub can’t show the commit, and waits for a full SHA', async () => {
    const fetcher = github({}, '', 404);
    const { unmount } = show('abc');
    expect(fetcher).not.toHaveBeenCalled();
    unmount();
    show();
    expect(await screen.findByText(/Couldn’t read this commit on GitHub/)).toBeVisible();
    github({}, '', 403);
    show('b'.repeat(40));
    expect(await screen.findByText(/GitHub’s hourly limit was reached/)).toBeVisible();
  });
});
