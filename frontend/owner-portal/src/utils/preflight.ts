import type { Configuration } from '../api';
import { GitHubError, githubGet, githubJson, githubRepository } from './github';
import {
  describeRequest,
  runtimeNames,
  runtimeRequest,
  RuntimeVersionError,
  VERSION_FILE_BYTES,
} from './runtimeVersions';

/**
 * Checks a commit against the rules the build enforces (deployment_config.py
 * validate_checkout), before anyone waits for a build. Advisory: the build
 * still decides. tests/test_checkout_preflight_parity.py and
 * preflight.test.ts run the same cases (preflight-cases.json) through both.
 */
export type CheckState = 'ok' | 'problem' | 'unknown';
export type CommitCheck = {
  /**
   * Stable identifier: package-json, script:<name>, lockfile:<package>, and
   * runtime-version for the Node.js or Bun version a commit asks for, or
   * runtime-default when it asks for none.
   */
  id: string;
  label: string;
  state: CheckState;
  /** What to fix, when state is "problem". */
  problem?: string;
};

export const PACKAGE_JSON_BYTES = 65_536;
export const LOCKFILE_BYTES = 1_048_576;

type Entry = { type: string; mode: string; size?: number };
/** A commit's files: the recursive tree, plus how to read a root file. */
export type Snapshot = {
  entries: Map<string, Entry>;
  /** GitHub truncates very large trees; a missing path then proves nothing. */
  complete: boolean;
  /** package.json, or a version file such as .nvmrc. */
  readFile: (name: string) => Promise<string>;
};

const SYMLINK = '120000';

/** Throws on duplicate object keys, which the build's strict JSON rejects. */
function rejectDuplicateKeys(text: string) {
  const frames: { keys: Set<string> | null; expectKey: boolean }[] = [];
  for (const [token] of text.matchAll(/"(?:[^"\\]|\\.)*"|[{}[\],:]/g)) {
    const top = frames[frames.length - 1];
    if (token === '{') frames.push({ keys: new Set(), expectKey: true });
    else if (token === '[') frames.push({ keys: null, expectKey: false });
    else if (token === '}' || token === ']') frames.pop();
    else if (token === ',') {
      if (top?.keys) top.expectKey = true;
    } else if (token !== ':' && top?.keys && top.expectKey) {
      const key = JSON.parse(token) as string;
      if (top.keys.has(key)) throw new SyntaxError('duplicate key');
      top.keys.add(key);
      top.expectKey = false;
    }
  }
}

class Unchecked extends Error {}

/** What runtime version the commit asks for, or why it can't be built. */
async function checkRuntime(
  check: CommitCheck,
  snapshot: Snapshot,
  configuration: Configuration,
  pkg: Record<string, unknown>,
) {
  const runtime = configuration.build.runtime;
  const read = async (name: string) => {
    const file = snapshot.entries.get(name);
    if (!file) {
      if (snapshot.complete) return null;
      throw new Unchecked();
    }
    if (file.type !== 'blob' || file.mode === SYMLINK || (file.size ?? 0) > VERSION_FILE_BYTES)
      throw new RuntimeVersionError(`${name} must be a file of at most 1 KB.`);
    return snapshot.readFile(name);
  };
  try {
    const request = await runtimeRequest(runtime, pkg, read);
    if (request) Object.assign(check, { label: describeRequest(request), state: 'ok' });
    else
      Object.assign(check, {
        id: 'runtime-default',
        label: `${runtimeNames[runtime]} (platform default)`,
        state: 'ok',
      });
  } catch (error) {
    // An unread or unlisted file leaves the check unknown.
    if (error instanceof RuntimeVersionError)
      Object.assign(check, { state: 'problem', problem: error.message });
  }
}

function where(directory: string) {
  return directory === '.' ? 'the repository root' : directory;
}

/** Normalizes like the build: no trailing slash, "." for the root. */
function normalized(directory: string) {
  return directory.replace(/\/+$/, '') || '.';
}

export async function checkSnapshot(
  snapshot: Snapshot,
  configuration: Configuration,
): Promise<CommitCheck[]> {
  const { entries, complete } = snapshot;
  const missing = (): CheckState => (complete ? 'problem' : 'unknown');
  const checks: CommitCheck[] = [];
  const bun = configuration.build.runtime === 'bun';
  const lockNames = bun ? ['bun.lock', 'bun.lockb'] : ['package-lock.json'];
  for (const raw of configuration.build.packages) {
    const directory = normalized(raw);
    const check: CommitCheck = {
      id: `lockfile:${directory}`,
      label: `Lockfile in ${where(directory)}`,
      state: 'ok',
    };
    checks.push(check);
    const folder = directory === '.' ? { type: 'tree', mode: '040000' } : entries.get(directory);
    if (folder?.mode === SYMLINK) {
      // The build follows links inside the repository; GitHub's tree doesn't.
      check.state = 'unknown';
      continue;
    }
    if (!folder || folder.type !== 'tree') {
      check.state = folder ? 'problem' : missing();
      check.problem = `${directory} isn’t a folder in this commit.`;
      continue;
    }
    const files = lockNames.map((name) =>
      entries.get(directory === '.' ? name : `${directory}/${name}`),
    );
    const found = files.find((file) => file?.type === 'blob' && file.mode !== SYMLINK);
    if (!found) {
      check.state = missing();
      check.problem = `Commit ${bun ? 'bun.lock' : 'package-lock.json'} in ${where(directory)}.`;
    } else if (
      !files.some(
        (file) =>
          file?.type === 'blob' && file.mode !== SYMLINK && (file.size ?? 0) <= LOCKFILE_BYTES,
      )
    ) {
      check.state = 'problem';
      check.problem = `The lockfile in ${where(directory)} is larger than 1 MB.`;
    }
  }

  const manifest: CommitCheck = {
    id: 'package-json',
    label: 'package.json in the repository root',
    state: 'ok',
  };
  checks.unshift(manifest);
  const scripts = [
    configuration.build.startScript,
    ...(configuration.build.buildScript ? [configuration.build.buildScript] : []),
  ].map((name): CommitCheck => ({
    id: `script:${name}`,
    label: `Script "${name}"`,
    state: 'unknown',
  }));
  const runtime: CommitCheck = {
    id: 'runtime-version',
    label: `${runtimeNames[configuration.build.runtime]} version`,
    state: 'unknown',
  };
  checks.splice(1, 0, ...scripts, runtime);
  const file = entries.get('package.json');
  if (!file || file.type !== 'blob') {
    manifest.state = file ? 'problem' : missing();
    manifest.problem = 'Add package.json to the repository root. The build reads scripts from it.';
    return checks;
  }
  if (file.mode === SYMLINK) {
    manifest.state = 'problem';
    manifest.problem = 'package.json must be a file, not a link.';
    return checks;
  }
  if ((file.size ?? 0) > PACKAGE_JSON_BYTES) {
    manifest.state = 'problem';
    manifest.problem = 'package.json must be at most 64 KB.';
    return checks;
  }
  let value: unknown;
  try {
    const text = await snapshot.readFile('package.json');
    value = JSON.parse(text);
    rejectDuplicateKeys(text);
  } catch (error) {
    if (error instanceof GitHubError) {
      manifest.state = 'unknown';
      return checks;
    }
    manifest.state = 'problem';
    manifest.problem = 'package.json isn’t valid JSON.';
    return checks;
  }
  const found =
    value && typeof value === 'object' && !Array.isArray(value)
      ? (value as Record<string, unknown>).scripts
      : undefined;
  if (!found || typeof found !== 'object' || Array.isArray(found)) {
    manifest.state = 'problem';
    manifest.problem = 'package.json needs a "scripts" section.';
    return checks;
  }
  for (const check of scripts) {
    const name = check.id.slice('script:'.length);
    const command = (found as Record<string, unknown>)[name];
    check.state = typeof command === 'string' && command ? 'ok' : 'problem';
    if (check.state === 'problem') check.problem = `package.json has no "${name}" script.`;
  }
  await checkRuntime(runtime, snapshot, configuration, value as Record<string, unknown>);
  return checks;
}

/** A public GitHub commit's files, read from the browser (private ones can't be). */
export async function githubSnapshot(
  repository: string,
  sha: string,
  signal?: AbortSignal,
): Promise<Snapshot> {
  const name = githubRepository(repository);
  if (!name || !/^[a-f0-9]{40}$/.test(sha)) throw new GitHubError('not-found');
  const data = await githubJson(
    `https://api.github.com/repos/${name}/git/trees/${sha}?recursive=1`,
    signal,
  );
  const tree = data && typeof data === 'object' ? (data as Record<string, unknown>).tree : null;
  if (!Array.isArray(tree)) throw new GitHubError('unavailable');
  const entries = new Map<string, Entry>();
  for (const item of tree) {
    if (!item || typeof item !== 'object') continue;
    const { path, type, mode, size } = item as Record<string, unknown>;
    if (typeof path === 'string' && typeof type === 'string' && typeof mode === 'string')
      entries.set(path, { type, mode, size: typeof size === 'number' ? size : undefined });
  }
  return {
    entries,
    complete: (data as Record<string, unknown>).truncated !== true,
    readFile: async (file) =>
      (await githubGet(`https://raw.githubusercontent.com/${name}/${sha}/${file}`, signal)).text(),
  };
}

/** Checks a public GitHub commit; throws GitHubError when it can't be read. */
export async function checkCommit(
  repository: string,
  sha: string,
  configuration: Configuration,
  signal?: AbortSignal,
) {
  return checkSnapshot(await githubSnapshot(repository, sha, signal), configuration);
}
