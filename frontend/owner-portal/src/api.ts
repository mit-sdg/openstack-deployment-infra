import type { RecentCommit } from './utils/github';
import type { CommitCheck } from './utils/preflight';

export type SourceReadOptions = {
  id: string;
  revision: number;
  scope?: 'admin';
  service: Pick<
    ReturnType<typeof resourceApi>,
    'sourceKey' | 'recentSourceCommits' | 'checkSourceCommit'
  >;
};

export const configurationGuidance = {
  root: 'The repository root must contain package.json.',
  versions:
    'Choose the version in package.json: engines.node or engines.bun, or "packageManager": "bun@1.3.4". Without one, builds use the platform default.',
  locks:
    'Each package directory must contain its runtime lockfile: package-lock.json for Node.js, bun.lock or bun.lockb for Bun.',
  scripts: 'Build and start scripts come from the root package.json.',
  health:
    'Use a health endpoint such as /health that returns HTTP 2xx with a body of at most 4 KB.',
  postgres: 'PostgreSQL’s DATABASE_URL already includes the password.',
};
export type StorageBinding = { resourceId: string; outputs: Record<string, string> };
export type StorageResource = {
  resourceId: string;
  type: 'postgres' | 'mongo' | 's3';
  label: string;
  status: string;
  createdAt: string;
  verifiedAt: string | null;
  defaultBindings: Record<string, string>;
};
export type Environment = {
  revision: number;
  intents?: Intent[];
  updatedAt: string | null;
  items: { name: string; updatedAt: string | null }[];
};
export type Configuration = {
  schemaVersion: 1;
  build: {
    runtime: 'node' | 'bun';
    packages: string[];
    buildScript: string | null;
    startScript: string;
  };
  runtime: { port: number; healthPath: string };
  storageBindings: StorageBinding[];
};
export type Settings = {
  revision: number;
  repository: string;
  branch: string;
  configuration: Configuration;
  configurationSha256: string | null;
};
export type AppRecord = {
  applicationId: string;
  /** "member" for a teammate's app; absent from older brokers. */
  access?: 'owner' | 'member' | 'admin';
  /** The owner's name, on apps you're a team member of. */
  ownerDisplayName?: string | null;
  slug: string;
  url: string | null;
  savedRevision: number;
  configurationChanged?: boolean;
  identityProvider?: boolean;
  lifecycleState: string;
  desiredRunning: boolean;
  activeDeploymentId: string | null;
  acceptedDeployment: { deploymentId: string; sourceCommit: string; acceptedAt: string } | null;
  health: {
    allocationHealthy: boolean | null;
    routeHealthy: boolean | null;
    schedulerState: string;
  } | null;
  stale: boolean;
  observedAt: string | null;
};
export type Intent = {
  intentId: string;
  appId: string;
  appSlug: string | null;
  commit: string | null;
  createdAt: string;
  kind: string;
  state: string;
  operationId: string | null;
  operation: { status: string; phase: string; cleanupState: string } | null;
  safeError: string | null;
  controllerErrorCode?: string | null;
  names?: string[];
  requiresResubmit?: boolean;
  retryKey?: string | null;
  /** Who made the change, in an app's activity. */
  actor?: { displayName: string | null; you: boolean };
};
/** Someone who works on an app: its owner, or a team member. */
export type TeamMember = {
  userId: string;
  username: string;
  displayName: string;
  method: 'local' | 'provider';
  role: 'owner' | 'member';
  addedAt: string | null;
};
const teamData = (v: unknown) => {
  const data = record(v);
  if (!Array.isArray(data.items)) throw new Error('Invalid service response');
  data.items.forEach((item) =>
    fields(item, { userId: 'string', username: 'string', displayName: 'string', role: 'string' }),
  );
  return data as { items: TeamMember[]; you?: string; access?: string; left?: boolean };
};
/** The runtime a deployment's build used: an exact release, or the platform default. */
export type DeploymentRuntime = {
  runtime: 'node' | 'bun';
  /** null for the platform default image, whose release isn't recorded. */
  version: string | null;
  image: string;
  /** Where the request was: "engines.node >=22 <23", "packageManager bun@1.3.4" or "default". */
  source: string;
};
export type Deployment = {
  deploymentId: string;
  applicationId: string;
  status: string;
  repositoryCommit: string;
  sourceRepository: string;
  configurationRevision: number;
  configuration: Configuration;
  configurationSha256: string;
  imageDigest: string | null;
  /** Absent from older platforms, and null until the build finishes. */
  runtime?: DeploymentRuntime | null;
  cleanupState: string;
  requestedAt: string;
  acceptedAt: string | null;
};
/** Usage and limits; `limit` is null for accounts without limits (admins). */
export type Quota = {
  apps: { used: number; reserved: number; limit: number | null };
  concurrentOperations: { used: number; reserved: number; limit: number | null };
};
export type Session = {
  user: { id: string; username: string; displayName: string };
  csrfToken: string;
  quota: Quota;
  expiresAt: string;
  role: 'owner' | 'staff' | 'admin';
  stepUpExpiresAt: string | null;
  /** Brand shown in the shell; absent from older brokers. */
  platformName?: string;
};
export type Page<T> = { items: T[]; nextCursor: string | null; truncated: boolean };
export type BuildLog = {
  text: string;
  state: string;
  nextOffset: number | null;
  truncated: boolean;
};
/** How a removed candidate stopped: Nomad task events and output tails. */
export type StartupRecord = {
  captured: boolean;
  /** False when the app never got a place to run. */
  found?: boolean;
  clientStatus?: string;
  restarts?: number;
  events?: { type: string; message: string; exitCode: number | null; oomKilled: boolean }[];
  stdout?: string;
  stderr?: string;
  capturedAt?: string | null;
};
/** An app's deploy key for a private repository; only the public half. */
export type SourceKey =
  { present: false } | { present: true; publicKey: string; fingerprint: string; createdAt: string };
/** Whether GitHub accepts the deploy key for the saved repository and branch. */
export type SourceAccess =
  | { keyPresent: false }
  | {
      keyPresent: true;
      reachable: boolean;
      head: string | null;
      branch: string;
      problem: 'key-refused' | 'not-found' | 'branch-missing' | 'unavailable' | null;
    };
/** stdout ("Output") or stderr ("Errors") of the running app. */
export type LogStream = 'stdout' | 'stderr';
/** Recent output of an app; `running` is false when nothing runs to read from. */
export type RuntimeLog = {
  stream: LogStream;
  running: boolean;
  text: string;
  truncated: boolean;
  lines: number;
  observedAt: string;
};
export const runtimeLogData = (v: unknown) =>
  fields(v, {
    stream: 'string',
    running: 'boolean',
    text: 'string',
    truncated: 'boolean',
    lines: 'number',
    observedAt: 'string',
  }) as RuntimeLog;

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public retryAfterSeconds = 30,
  ) {
    super(message);
  }
}
let csrf = '';
let credentialEpoch = 0;
export function clearCredentials() {
  csrf = '';
  credentialEpoch++;
}
export function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw new Error('Invalid service response');
  return value as Record<string, unknown>;
}
export function fields(value: unknown, checks: Record<string, 'string' | 'number' | 'boolean'>) {
  const data = record(value);
  for (const [key, type] of Object.entries(checks))
    if (typeof data[key] !== type) throw new Error('Invalid service response');
  return data;
}
const appData = (v: unknown) =>
  fields(v, {
    applicationId: 'string',
    slug: 'string',
    savedRevision: 'number',
    desiredRunning: 'boolean',
    stale: 'boolean',
  }) as AppRecord;
const intentData = (v: unknown) =>
  fields(v, { intentId: 'string', appId: 'string', kind: 'string', state: 'string' }) as Intent;
const deploymentData = (v: unknown) =>
  fields(v, {
    deploymentId: 'string',
    applicationId: 'string',
    repositoryCommit: 'string',
    status: 'string',
    configurationRevision: 'number',
  }) as Deployment;
export function pageData<T>(value: unknown, decode: (item: unknown) => T): Page<T> {
  const data = record(value);
  if (
    !Array.isArray(data.items) ||
    (data.nextCursor !== null && typeof data.nextCursor !== 'string') ||
    typeof data.truncated !== 'boolean'
  )
    throw new Error('Invalid page response');
  return {
    items: data.items.map(decode),
    nextCursor: data.nextCursor as string | null,
    truncated: data.truncated,
  };
}
// Keep metadata panels inside the broker's two-active-reads account bound.
let adminReads = 0;
const adminReadWaiters: (() => void)[] = [];
async function adminReadSlot() {
  if (adminReads >= 2) await new Promise<void>((resolve) => adminReadWaiters.push(resolve));
  else adminReads++;
  return () => {
    const next = adminReadWaiters.shift();
    if (next) next();
    else adminReads--;
  };
}
export async function request<T>(
  path: string,
  decode: (v: unknown) => T,
  options?: { method: string; body: unknown; key?: string },
  refreshed = false,
  signal?: AbortSignal,
): Promise<T> {
  const epoch = credentialEpoch;
  const staff =
    path.startsWith('/admin-apps') ||
    path.startsWith('/staff/') ||
    path.startsWith('/accounts') ||
    path.startsWith('/account-audit');
  const release = !options && path.startsWith('/admin-apps') ? await adminReadSlot() : () => {};
  let response: Response;
  try {
    if (epoch !== credentialEpoch)
      throw new ApiError(401, 'SESSION_EXPIRED', 'Sign in to continue.');
    response = await fetch(`/api/v1${path}`, {
      credentials: 'same-origin',
      cache: 'no-store',
      signal,
      ...(staff ? { headers: { 'X-CSRF-Token': csrf } } : {}),
      ...(options
        ? {
            method: options.method,
            headers: {
              'Content-Type': 'application/json',
              'X-CSRF-Token': csrf,
              ...(options.key ? { 'Idempotency-Key': options.key } : {}),
            },
            body: JSON.stringify(options.body),
          }
        : {}),
    });
  } finally {
    release();
  }
  if (response.status === 204) return undefined as T;
  const payload = record(await response.json());
  if (epoch !== credentialEpoch) throw new ApiError(401, 'SESSION_EXPIRED', 'Sign in to continue.');
  if (!response.ok) {
    const error = record(payload.error);
    if (
      (options || staff) &&
      !refreshed &&
      response.status === 403 &&
      error.code === 'CSRF_REJECTED'
    ) {
      await api.session();
      return request(path, decode, options, true, signal);
    }
    if (response.status === 401 || error.code === 'ACCOUNT_DISABLED') {
      clearCredentials();
      window.dispatchEvent(new Event('portal-session-ended'));
    }
    throw new ApiError(
      response.status,
      typeof error.code === 'string' ? error.code : 'UNAVAILABLE',
      typeof error.summary === 'string' ? error.summary : 'The service is unavailable.',
      typeof error.retryAfterSeconds === 'number' &&
        error.retryAfterSeconds > 0 &&
        error.retryAfterSeconds <= 300
        ? error.retryAfterSeconds
        : 30,
    );
  }
  return decode(payload.data);
}
export const api = {
  ...resourceApi(),
  session: (signal?: AbortSignal) =>
    request(
      '/session',
      (v) => {
        const data = fields(v, { csrfToken: 'string', expiresAt: 'string' });
        fields(data.user, { id: 'string', displayName: 'string', username: 'string' });
        if (!['owner', 'staff', 'admin'].includes(String(data.role)))
          throw new Error('Invalid session role');
        csrf = data.csrfToken as string;
        if (typeof data.platformName !== 'string' || !data.platformName) delete data.platformName;
        return data as Session;
      },
      undefined,
      false,
      signal,
    ),
  logout: () => request('/logout', () => undefined, { method: 'POST', body: {} }),
  apps: () =>
    request('/apps', (v) => ({ ...pageData(v, appData), quota: record(v).quota as Quota })),
  app: (id: string) => request(`/apps/${id}`, appData),
  create: (slug: string, key: string) =>
    request(
      '/apps',
      (v) => ({ app: appData(record(v).app), intent: intentData(record(v).intent) }),
      { method: 'POST', body: { slug }, key },
    ),
  state: (id: string, desiredRunning: boolean, key: string, identityProviderConfirmed = false) =>
    request(`/apps/${id}/state`, intentData, {
      method: 'POST',
      body: { desiredRunning, ...(identityProviderConfirmed ? { identityProviderConfirmed } : {}) },
      key,
    }),
  restart: (id: string, key: string, identityProviderConfirmed = false) =>
    request(`/apps/${id}/restart`, intentData, {
      method: 'POST',
      body: identityProviderConfirmed ? { identityProviderConfirmed } : {},
      key,
    }),
  deploy: (
    id: string,
    revision: number,
    commit: string,
    key: string,
    identityProviderConfirmed = false,
  ) =>
    request(`/apps/${id}/deployments`, intentData, {
      method: 'POST',
      body: {
        configurationRevision: revision,
        commit,
        ...(identityProviderConfirmed ? { identityProviderConfirmed: true } : {}),
      },
      key,
    }),
  history: (id: string, cursor?: string) =>
    request(
      `/apps/${id}/deployments${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`,
      (v) => pageData(v, deploymentData),
    ),
  deployment: (app: string, deployment: string) =>
    request(`/apps/${app}/deployments/${deployment}`, deploymentData),
  log: (app: string, deployment: string) =>
    request(
      `/apps/${app}/deployments/${deployment}/build-log?lines=200`,
      (v) => fields(v, { text: 'string', state: 'string', truncated: 'boolean' }) as BuildLog,
    ),
  startupLog: (app: string, deployment: string) =>
    request(`/apps/${app}/deployments/${deployment}/startup-log`, (v) => {
      const data = fields(v, { captured: 'boolean' }) as StartupRecord;
      if (data.captured) {
        fields(v, { found: 'boolean', stdout: 'string', stderr: 'string', restarts: 'number' });
        if (!Array.isArray(data.events)) throw new Error('Invalid service response');
      }
      return data;
    }),
  activity: (app: string) =>
    request(`/apps/${app}/activity?limit=8`, (v) => {
      const data = record(v);
      if (!Array.isArray(data.items)) throw new Error('Invalid service response');
      return data.items.map(intentData);
    }),
  logs: (app: string, stream: LogStream) =>
    request(`/apps/${app}/logs?stream=${stream}`, runtimeLogData),
  intents: () => request('/intents?limit=8', (v) => pageData(v, intentData)),
  intent: (id: string) => request(`/intents/${id}`, intentData),
  resume: (id: string) =>
    request(`/intents/${id}/resume`, intentData, { method: 'POST', body: {} }),
};
export function validateSettings(settings: Settings): string | null {
  if (!/^https:\/\/github\.com\/[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/.test(settings.repository))
    return 'Enter a GitHub repository URL like https://github.com/owner/repo, without credentials or query parameters.';
  if (
    !/^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$/.test(settings.branch) ||
    /\.\.|\/\/|@\{|\.lock$|[/.]$/.test(settings.branch)
  )
    return 'Enter a valid preferred branch.';
  if (
    !settings.configuration.build.packages.length ||
    settings.configuration.build.packages.length > 32 ||
    settings.configuration.build.packages.some(
      (p) => p !== '.' && (!p || p.startsWith('/') || p.includes('..') || p.includes('\\')),
    ) ||
    new Set(settings.configuration.build.packages).size !==
      settings.configuration.build.packages.length
  )
    return `Package directories must be unique paths inside your repository. ${configurationGuidance.locks}`;
  if (
    !/^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$/.test(settings.configuration.build.startScript) ||
    (settings.configuration.build.buildScript !== null &&
      !/^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$/.test(settings.configuration.build.buildScript))
  )
    return `Enter script names, rather than commands. ${configurationGuidance.scripts}`;
  const { port, healthPath } = settings.configuration.runtime;
  if (!Number.isInteger(port) || port < 1 || port > 65535)
    return 'Port must be between 1 and 65535.';
  if (
    !healthPath.startsWith('/') ||
    healthPath.startsWith('//') ||
    /[?#\\\s]/.test(healthPath) ||
    healthPath.split('/').includes('..') ||
    healthPath.length > 256
  )
    return `Enter an absolute health path without a query or fragment. ${configurationGuidance.health}`;
  return null;
}

export function validateEnvName(name: string): string | null {
  if (!/^[A-Z][A-Z0-9_]{0,127}$/.test(name))
    return 'Use A–Z, 0–9 and underscores, starting with A–Z (at most 128 characters).';
  if (
    [
      'NODE_ENV',
      'NODE_EXTRA_CA_CERTS',
      'PLATFORM_ENV',
      'PLATFORM_PROJECT_ID',
      'PLATFORM_PROJECT_SLUG',
      'PORT',
    ].includes(name) ||
    name.startsWith('STORAGE__')
  )
    return 'This name is reserved by the platform.';
  return null;
}
export function validateBindings(bindings: StorageBinding[], names: string[]): string | null {
  const targets = new Set<string>();
  for (const binding of bindings) {
    for (const name of Object.values(binding.outputs)) {
      const invalid = validateEnvName(name);
      if (invalid) return invalid;
      if (targets.has(name)) return `${name} is used by more than one output.`;
      if (names.includes(name)) return `${name} already exists as an environment variable.`;
      targets.add(name);
    }
  }
  return null;
}

/** Thrown when someone declines a storage confirmation; never shown as an error. */
export class ActionCanceled extends Error {
  constructor() {
    super('Action canceled.');
  }
}

/**
 * Asks to confirm a storage change on the app that runs portal sign-in, for
 * example in a Dialog. Resolve true to continue.
 */
export type ConfirmStorage = () => boolean | Promise<boolean>;

// Both workspaces use the same resource requests and write-only controls.
// Owner requests confirm only for the sign-in app; admin requests let the
// callback decide. There is no native confirm fallback: owner storage changes
// on the sign-in app need a ConfirmStorage callback.
export function resourceApi(prefix = '/apps', confirmStorage?: ConfirmStorage) {
  async function consentFields(id: string) {
    if (prefix === '/apps') {
      if (!(await api.app(id)).identityProvider) return {};
      if (!confirmStorage)
        throw new Error(
          'Portal sign-in depends on this app. Confirm storage changes from its settings page.',
        );
    } else if (!confirmStorage) return {};
    if (!(await confirmStorage())) throw new ActionCanceled();
    return { identityProviderConfirmed: true };
  }
  return {
    settings: (id: string) =>
      request(
        `${prefix}/${id}/configuration`,
        (v) =>
          fields(v, { revision: 'number', repository: 'string', branch: 'string' }) as Settings,
      ),
    save: (id: string, settings: Settings, key: string) =>
      request(`${prefix}/${id}/configuration`, (v) => fields(v, { revision: 'number' }), {
        method: 'PUT',
        body: {
          expectedRevision: settings.revision,
          repository: settings.repository,
          branch: settings.branch,
          configuration: settings.configuration,
        },
        key,
      }),
    environment: (id: string) =>
      request(`${prefix}/${id}/environment`, (v) => record(v) as Environment),
    setEnvironment: (id: string, name: string, value: string, key: string) =>
      request(`${prefix}/${id}/environment/${name}`, intentData, {
        method: 'PUT',
        body: { value },
        key,
      }),
    deleteEnvironment: (id: string, name: string, key: string) =>
      request(`${prefix}/${id}/environment/${name}`, intentData, {
        method: 'DELETE',
        body: {},
        key,
      }),
    storage: (id: string) =>
      request(
        `${prefix}/${id}/storage`,
        (v) =>
          record(v) as { items: StorageResource[]; intents: (Intent & { type: string | null })[] },
      ),
    createStorage: async (id: string, type: StorageResource['type'], key: string) =>
      request(`${prefix}/${id}/storage`, intentData, {
        method: 'POST',
        body: { type, ...(await consentFields(id)) },
        key,
      }),
    storageAction: async (id: string, resource: string, action: 'verify' | 'rotate', key: string) =>
      request(`${prefix}/${id}/storage/${resource}/${action}`, intentData, {
        method: 'POST',
        body: await consentFields(id),
        key,
      }),
    members: (id: string) => request(`${prefix}/${id}/members`, teamData),
    addMember: (id: string, username: string) =>
      request(`${prefix}/${id}/members`, teamData, { method: 'POST', body: { username } }),
    removeMember: (id: string, userId: string) =>
      request(`${prefix}/${id}/members/${userId}`, teamData, { method: 'DELETE', body: {} }),
    recentSourceCommits: (id: string) =>
      request(
        `${prefix}/${id}/source/commits`,
        (v) => {
          const data = fields(v, { keyPresent: 'boolean' });
          if (!data.keyPresent) return [] as RecentCommit[];
          if (!Array.isArray(data.items) || data.items.length > 5)
            throw new Error('Invalid service response');
          return data.items.map((item) => {
            const commit = fields(item, { sha: 'string', message: 'string' });
            if (!/^[a-f0-9]{40}$/.test(commit.sha as string))
              throw new Error('Invalid service response');
            return commit as RecentCommit;
          });
        },
        { method: 'POST', body: {} },
      ),
    checkSourceCommit: (id: string, commit: string, configurationRevision: number) =>
      request(
        `${prefix}/${id}/source/check`,
        (v) => {
          const data = fields(v, { keyPresent: 'boolean' });
          if (!data.keyPresent) throw new Error('No deploy key');
          if (!Array.isArray(data.items) || data.items.length > 102)
            throw new Error('Invalid service response');
          return data.items.map((item) => {
            const check = fields(item, { id: 'string', label: 'string', state: 'string' });
            if (!['ok', 'problem', 'unknown'].includes(check.state as string))
              throw new Error('Invalid service response');
            if (check.state === 'problem') fields(item, { problem: 'string' });
            return check as CommitCheck;
          });
        },
        { method: 'POST', body: { commit, configurationRevision } },
      ),
    sourceKey: (id: string) =>
      request(`${prefix}/${id}/source-key`, (v) => {
        const data = fields(v, { present: 'boolean' });
        if (data.present) fields(v, { publicKey: 'string', fingerprint: 'string' });
        return data as SourceKey;
      }),
    createSourceKey: (id: string, replace = false) =>
      request(
        `${prefix}/${id}/source-key`,
        (v) => fields(v, { present: 'boolean', publicKey: 'string' }) as SourceKey,
        { method: 'POST', body: replace ? { replace: true } : {} },
      ),
    removeSourceKey: (id: string) =>
      request(`${prefix}/${id}/source-key`, (v) => fields(v, { present: 'boolean' }) as SourceKey, {
        method: 'DELETE',
        body: {},
      }),
    checkSourceKey: (id: string) =>
      request(
        `${prefix}/${id}/source-key/check`,
        (v) => fields(v, { keyPresent: 'boolean' }) as SourceAccess,
        { method: 'POST', body: {} },
      ),
  };
}
