// @vitest-environment node
import { describe, expect, it, vi } from 'vitest';
import { GitHubError, githubRepository, recentCommits } from './github';

const sha = (digit: string) => digit.repeat(40);
function commit(digit: string, message: string, extra: Record<string, unknown> = {}) {
  return {
    sha: sha(digit),
    commit: {
      message,
      author: { name: 'Ada Student', date: '2026-10-03T12:00:00Z' },
      committer: { name: 'GitHub', date: '2026-10-03T12:05:00Z' },
      ...extra,
    },
  };
}
function respond(status: number, body: unknown) {
  const fetcher = vi.fn(() =>
    Promise.resolve(
      new Response(typeof body === 'string' ? body : JSON.stringify(body), { status }),
    ),
  );
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}
async function problem(promise: Promise<unknown>) {
  const error = await promise.catch((caught: unknown) => caught);
  expect(error).toBeInstanceOf(GitHubError);
  return (error as GitHubError).problem;
}

describe('recent commits from GitHub', () => {
  it('reads the branch without cookies or a referrer and keeps first lines', async () => {
    const fetcher = respond(200, [
      commit('a', 'Fix the login form\n\nLonger explanation'),
      { sha: 'not-a-sha', commit: { message: 'skipped' } },
      commit('b', '', { author: null, committer: null }),
    ]);
    const commits = await recentCommits('https://github.com/ada/notes-app', 'feature/x y');
    expect(commits).toEqual([
      {
        sha: sha('a'),
        message: 'Fix the login form',
        author: 'Ada Student',
        date: '2026-10-03T12:05:00Z',
      },
      { sha: sha('b'), message: '(no message)', author: null, date: null },
    ]);
    const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(
      'https://api.github.com/repos/ada/notes-app/commits?sha=feature%2Fx+y&per_page=5',
    );
    expect(init).toMatchObject({ credentials: 'omit', referrerPolicy: 'no-referrer' });
  });

  it('turns GitHub answers into a reason to paste a SHA', async () => {
    for (const [status, body, reason] of [
      [404, { message: 'Not Found' }, 'not-found'],
      [409, { message: 'Git Repository is empty.' }, 'not-found'],
      [422, { message: 'No commit found for SHA: main' }, 'not-found'],
      [403, { message: 'API rate limit exceeded' }, 'rate-limited'],
      [429, {}, 'rate-limited'],
      [500, {}, 'unavailable'],
      [200, { not: 'a list' }, 'unavailable'],
      [200, 'not json', 'unavailable'],
    ] as const) {
      respond(status, body);
      expect(await problem(recentCommits('https://github.com/ada/app', 'main'))).toBe(reason);
    }
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new TypeError('offline'))),
    );
    expect(await problem(recentCommits('https://github.com/ada/app', 'main'))).toBe('unavailable');
  });

  it('only asks GitHub about canonical repository URLs', async () => {
    const fetcher = respond(200, []);
    expect(githubRepository('https://github.com/ada/app')).toBe('ada/app');
    for (const url of [
      'https://github.com/ada/app/',
      'https://github.com/ada',
      'https://example.com/ada/app',
      'https://github.com/ada/app?tab=1',
    ]) {
      expect(githubRepository(url)).toBeNull();
      expect(await problem(recentCommits(url, 'main'))).toBe('not-found');
    }
    expect(fetcher).not.toHaveBeenCalled();
  });
});
