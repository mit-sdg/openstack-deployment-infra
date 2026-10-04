import { describe, expect, it, vi } from 'vitest';
import type { Configuration } from '../api';
import { GitHubError } from './github';
import { checkCommit, checkSnapshot, type Snapshot } from './preflight';
import rawCases from './preflight-cases.json';

type Case = {
  name: string;
  configuration: Configuration['build'];
  files: Record<string, string>;
  sizes?: Record<string, number>;
  links?: Record<string, string>;
  problems: string[];
};
const cases = rawCases as unknown as Case[];

function configuration(build: Case['configuration']): Configuration {
  return {
    schemaVersion: 1,
    build,
    runtime: { port: 3000, healthPath: '/health' },
    storageBindings: [],
  };
}

/** The commit as GitHub's recursive tree would list it. */
function snapshot(item: Case): Snapshot {
  const entries: Snapshot['entries'] = new Map();
  const files = item.files;
  const add = (path: string, mode: string, size: number) => {
    const parts = path.split('/');
    for (let index = 1; index < parts.length; index++)
      entries.set(parts.slice(0, index).join('/'), { type: 'tree', mode: '040000' });
    entries.set(path, { type: 'blob', mode, size });
  };
  for (const [path, content] of Object.entries(files))
    add(path, '100644', item.sizes?.[path] ?? new TextEncoder().encode(content).length);
  for (const [path, target] of Object.entries(item.links ?? {})) add(path, '120000', target.length);
  return {
    entries,
    complete: true,
    readPackageJson: () => Promise.resolve(files['package.json']),
  };
}

describe('commit preflight', () => {
  it.each(cases)('agrees with the build: $name', async (item) => {
    const checks = await checkSnapshot(snapshot(item), configuration(item.configuration));
    expect(
      checks
        .filter((check) => check.state === 'problem')
        .map((check) => check.id)
        .sort(),
    ).toEqual([...item.problems].sort());
    for (const check of checks) if (check.state === 'problem') expect(check.problem).toBeTruthy();
  });

  it('only claims a missing file when GitHub listed the whole commit', async () => {
    const item = cases.find((entry) => entry.name === 'node lockfile missing')!;
    const checks = await checkSnapshot(
      { ...snapshot(item), complete: false },
      configuration(item.configuration),
    );
    expect(checks.find((check) => check.id === 'lockfile:.')?.state).toBe('unknown');
  });

  it('reads the tree once from the API and package.json from raw files', async () => {
    const sha = 'a'.repeat(40);
    const fetcher = vi.fn((url: string) =>
      Promise.resolve(
        url.startsWith('https://api.github.com/')
          ? new Response(
              JSON.stringify({
                truncated: false,
                tree: [
                  { path: 'package.json', type: 'blob', mode: '100644', size: 40 },
                  { path: 'package-lock.json', type: 'blob', mode: '100644', size: 2 },
                ],
              }),
            )
          : new Response('{"scripts":{"start":"node ."}}'),
      ),
    );
    vi.stubGlobal('fetch', fetcher);
    const checks = await checkCommit(
      'https://github.com/ada/app',
      sha,
      configuration({ runtime: 'node', packages: ['.'], buildScript: null, startScript: 'start' }),
    );
    expect(checks.every((check) => check.state === 'ok')).toBe(true);
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      `https://api.github.com/repos/ada/app/git/trees/${sha}?recursive=1`,
      `https://raw.githubusercontent.com/ada/app/${sha}/package.json`,
    ]);
    for (const [, init] of fetcher.mock.calls as unknown as [string, RequestInit][])
      expect(init).toMatchObject({ credentials: 'omit', referrerPolicy: 'no-referrer' });
  });

  it('reports a private or missing commit as a GitHubError', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(new Response('{}', { status: 404 }))),
    );
    await expect(
      checkCommit(
        'https://github.com/ada/private',
        'b'.repeat(40),
        configuration({
          runtime: 'node',
          packages: ['.'],
          buildScript: null,
          startScript: 'start',
        }),
      ),
    ).rejects.toBeInstanceOf(GitHubError);
  });
});
