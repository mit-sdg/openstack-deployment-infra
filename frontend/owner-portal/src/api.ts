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
  slug: string;
  url: string | null;
  savedRevision: number;
  configurationChanged?: boolean;
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
  names?: string[];
  requiresResubmit?: boolean;
  retryKey?: string | null;
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
  cleanupState: string;
  requestedAt: string;
  acceptedAt: string | null;
};
export type Quota = {
  apps: { used: number; reserved: number; limit: number };
  concurrentOperations: { used: number; reserved: number; limit: number };
};
export type Session = {
  user: { id: string; username: string; displayName: string };
  csrfToken: string;
  quota: Quota;
  expiresAt: string;
  role: 'owner' | 'staff' | 'admin';
  stepUpExpiresAt: string | null;
};
export type Page<T> = { items: T[]; nextCursor: string | null; truncated: boolean };
export type BuildLog = {
  text: string;
  state: string;
  nextOffset: number | null;
  truncated: boolean;
};

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
export async function request<T>(
  path: string,
  decode: (v: unknown) => T,
  options?: { method: string; body: unknown; key?: string },
  refreshed = false,
  signal?: AbortSignal,
): Promise<T> {
  const epoch = credentialEpoch;
  const staff =
    path.startsWith('/staff/') || path.startsWith('/accounts') || path.startsWith('/account-audit');
  const response = await fetch(`/api/v1${path}`, {
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
  session: (signal?: AbortSignal) =>
    request(
      '/session',
      (v) => {
        const data = fields(v, { csrfToken: 'string', expiresAt: 'string' });
        fields(data.user, { id: 'string', displayName: 'string', username: 'string' });
        if (!['owner', 'staff', 'admin'].includes(String(data.role)))
          throw new Error('Invalid session role');
        csrf = data.csrfToken as string;
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
  settings: (id: string) =>
    request(
      `/apps/${id}/configuration`,
      (v) => fields(v, { revision: 'number', repository: 'string', branch: 'string' }) as Settings,
    ),
  save: (id: string, settings: Settings, key: string) =>
    request(`/apps/${id}/configuration`, (v) => fields(v, { revision: 'number' }), {
      method: 'PUT',
      body: {
        expectedRevision: settings.revision,
        repository: settings.repository,
        branch: settings.branch,
        configuration: settings.configuration,
      },
      key,
    }),
  environment: (id: string) => request(`/apps/${id}/environment`, (v) => record(v) as Environment),
  setEnvironment: (id: string, name: string, value: string, key: string) =>
    request(`/apps/${id}/environment/${name}`, intentData, { method: 'PUT', body: { value }, key }),
  deleteEnvironment: (id: string, name: string, key: string) =>
    request(`/apps/${id}/environment/${name}`, intentData, { method: 'DELETE', body: {}, key }),
  storage: (id: string) =>
    request(
      `/apps/${id}/storage`,
      (v) =>
        record(v) as { items: StorageResource[]; intents: (Intent & { type: string | null })[] },
    ),
  createStorage: (id: string, type: StorageResource['type'], key: string) =>
    request(`/apps/${id}/storage`, intentData, { method: 'POST', body: { type }, key }),
  storageAction: (id: string, resource: string, action: 'verify' | 'rotate', key: string) =>
    request(`/apps/${id}/storage/${resource}/${action}`, intentData, {
      method: 'POST',
      body: {},
      key,
    }),
  deploy: (id: string, revision: number, commit: string, key: string) =>
    request(`/apps/${id}/deployments`, intentData, {
      method: 'POST',
      body: { configurationRevision: revision, commit },
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
  intents: () => request('/intents?limit=8', (v) => pageData(v, intentData)),
  intent: (id: string) => request(`/intents/${id}`, intentData),
  resume: (id: string) =>
    request(`/intents/${id}/resume`, intentData, { method: 'POST', body: {} }),
};
export function validateSettings(settings: Settings): string | null {
  if (!/^https:\/\/github\.com\/[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/.test(settings.repository))
    return 'Enter a public GitHub repository URL without credentials or query parameters.';
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
    return 'Package directories must be unique paths inside your repository.';
  if (
    !/^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$/.test(settings.configuration.build.startScript) ||
    (settings.configuration.build.buildScript !== null &&
      !/^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$/.test(settings.configuration.build.buildScript))
  )
    return 'Enter package script names, rather than commands.';
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
    return 'Enter an absolute health path without a query or fragment.';
  return null;
}

export function validateEnvName(name: string): string | null {
  if (!/^[A-Z][A-Z0-9_]{0,127}$/.test(name))
    return 'Use A–Z, 0–9 and underscores, starting with A–Z (at most 128 characters).';
  if (
    ['NODE_ENV', 'PLATFORM_ENV', 'PLATFORM_PROJECT_ID', 'PLATFORM_PROJECT_SLUG', 'PORT'].includes(
      name,
    ) ||
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
