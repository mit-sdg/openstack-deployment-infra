// @vitest-environment node
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
    readFile: (name) => Promise.resolve(files[name]),
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

  it('names the runtime version a commit asks for, or the platform default', async () => {
    const runtime = async (name: string) => {
      const item = cases.find((entry) => entry.name === name)!;
      const checks = await checkSnapshot(snapshot(item), configuration(item.configuration));
      return checks.find((check) => check.id.startsWith('runtime-'));
    };
    expect(await runtime('engines.node asks for a supported version')).toEqual({
      id: 'runtime-version',
      label: 'Node.js >=22 <23 from engines.node',
      state: 'ok',
    });
    expect(await runtime('bun packageManager names an exact version')).toMatchObject({
      label: 'Bun 1.3.4 from packageManager',
    });
    expect(await runtime('node app at the root')).toEqual({
      id: 'runtime-default',
      label: 'Node.js (platform default)',
      state: 'ok',
    });
    expect(await runtime('engines.node asks for an end-of-life version')).toEqual({
      id: 'runtime-version',
      label: 'Node.js version',
      state: 'problem',
      problem:
        'engines.node ">=18 <19" asks for Node.js 18, older than the oldest supported version (20).',
    });
  });

  it('leaves the runtime version unknown when a version file can’t be read', async () => {
    const item = cases.find((entry) => entry.name === 'node version from .nvmrc')!;
    const unread: Snapshot = {
      ...snapshot(item),
      readFile: (name) =>
        name === 'package.json'
          ? Promise.resolve(item.files['package.json'])
          : Promise.reject(new GitHubError('unavailable')),
    };
    const truncated: Snapshot = { ...snapshot(cases[0]), complete: false };
    for (const [files, build] of [
      [unread, item.configuration],
      [truncated, cases[0].configuration],
    ] as const) {
      const checks = await checkSnapshot(files, configuration(build));
      expect(checks.find((check) => check.id === 'runtime-version')?.state).toBe('unknown');
    }
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

  it('reads a version file from raw files only when package.json asks for nothing', async () => {
    const sha = 'c'.repeat(40);
    const fetcher = vi.fn((url: string) =>
      Promise.resolve(
        url.startsWith('https://api.github.com/')
          ? new Response(
              JSON.stringify({
                truncated: false,
                tree: [
                  { path: 'package.json', type: 'blob', mode: '100644', size: 40 },
                  { path: 'package-lock.json', type: 'blob', mode: '100644', size: 2 },
                  { path: '.nvmrc', type: 'blob', mode: '100644', size: 3 },
                ],
              }),
            )
          : new Response(url.endsWith('/.nvmrc') ? '22\n' : '{"scripts":{"start":"node ."}}'),
      ),
    );
    vi.stubGlobal('fetch', fetcher);
    const checks = await checkCommit(
      'https://github.com/ada/app',
      sha,
      configuration({ runtime: 'node', packages: ['.'], buildScript: null, startScript: 'start' }),
    );
    expect(checks.find((check) => check.id === 'runtime-version')?.label).toBe(
      'Node.js 22 from .nvmrc',
    );
    expect(fetcher.mock.calls.map(([url]) => url).slice(1)).toEqual([
      `https://raw.githubusercontent.com/ada/app/${sha}/package.json`,
      `https://raw.githubusercontent.com/ada/app/${sha}/.nvmrc`,
    ]);
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
