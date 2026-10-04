/** A commit on a branch, read from GitHub's public API by the browser. */
export type RecentCommit = {
  sha: string;
  /** First line of the commit message. */
  message: string;
  author: string | null;
  date: string | null;
};

export type GitHubProblem = 'not-found' | 'rate-limited' | 'unavailable';

export class GitHubError extends Error {
  constructor(public problem: GitHubProblem) {
    super(problem);
  }
}

/** "owner/repo" for a canonical public GitHub URL, or null. */
export function githubRepository(url: string) {
  const match = /^https:\/\/github\.com\/([A-Za-z0-9._-]+)\/([A-Za-z0-9._-]+)$/.exec(url);
  return match ? `${match[1]}/${match[2]}` : null;
}

export function commitUrl(repository: string, sha: string) {
  return `https://github.com/${githubRepository(repository)}/commit/${sha}`;
}

const text = (value: unknown, limit: number) =>
  typeof value === 'string' && value.trim() ? value.trim().slice(0, limit) : null;

function parse(item: unknown): RecentCommit[] {
  if (!item || typeof item !== 'object') return [];
  const { sha, commit } = item as { sha?: unknown; commit?: unknown };
  if (
    typeof sha !== 'string' ||
    !/^[a-f0-9]{40}$/.test(sha) ||
    !commit ||
    typeof commit !== 'object'
  )
    return [];
  const { message, author, committer } = commit as Record<string, unknown>;
  const person = (value: unknown) =>
    value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
  const date = text(person(committer).date, 40) ?? text(person(author).date, 40);
  return [
    {
      sha,
      message: text(String(message ?? '').split('\n')[0], 200) ?? '(no message)',
      author: text(person(author).name, 100),
      date: date && !Number.isNaN(Date.parse(date)) ? date : null,
    },
  ];
}

/**
 * GET from GitHub straight from the browser, without cookies or a referrer.
 * Private repositories, unknown refs and GitHub's hourly limit for unsigned
 * API requests come back as a GitHubError.
 */
export async function githubGet(url: string, signal?: AbortSignal): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(url, {
      credentials: 'omit',
      referrerPolicy: 'no-referrer',
      cache: 'no-store',
      ...(url.startsWith('https://api.github.com/')
        ? { headers: { Accept: 'application/vnd.github+json' } }
        : {}),
      signal,
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new GitHubError('unavailable');
  }
  // 409: empty repository; 422: no such branch or commit.
  if ([404, 409, 422].includes(response.status)) throw new GitHubError('not-found');
  if (response.status === 403 || response.status === 429) throw new GitHubError('rate-limited');
  if (!response.ok) throw new GitHubError('unavailable');
  return response;
}

export async function githubJson(url: string, signal?: AbortSignal): Promise<unknown> {
  const response = await githubGet(url, signal);
  try {
    return await response.json();
  } catch {
    throw new GitHubError('unavailable');
  }
}

/** The newest commits on a branch of a public repository. */
export async function recentCommits(
  repository: string,
  branch: string,
  signal?: AbortSignal,
  count = 5,
): Promise<RecentCommit[]> {
  const name = githubRepository(repository);
  if (!name) throw new GitHubError('not-found');
  const data = await githubJson(
    `https://api.github.com/repos/${name}/commits?` +
      new URLSearchParams({ sha: branch, per_page: String(count) }),
    signal,
  );
  if (!Array.isArray(data)) throw new GitHubError('unavailable');
  return data.slice(0, count).flatMap(parse);
}
