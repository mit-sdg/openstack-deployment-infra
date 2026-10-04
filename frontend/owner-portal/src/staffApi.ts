import { pageData, record, request, type Page, type Quota } from './api';

export type StaffOwner = {
  ownerId: string;
  username: string;
  displayName: string;
  portalEnabled: boolean;
  role: 'owner' | 'staff' | 'admin';
};
/** Owner names that list rows carry, so lists need no per-owner reads. */
type OwnerName = { ownerUsername: string; ownerDisplayName: string };
export type StaffCatalogApp = {
  applicationId: string;
  ownerId: string;
  slug: string;
  lifecycleState: string;
  savedRevision: number;
  createdAt: string | null;
  repository: string | null;
};
export type StaffAppRow = StaffCatalogApp & OwnerName;
export type StaffApp = StaffCatalogApp & {
  url: string | null;
  desiredRunning: boolean;
  activeDeploymentId: string | null;
  acceptedDeployment: {
    deploymentId: string;
    sourceCommit: string | null;
    acceptedAt: string | null;
  } | null;
  health: { process: string; route: string };
  observedAt: string | null;
  stale: boolean;
};
export type StaffDeployment = {
  deploymentId: string;
  applicationId: string;
  status: string;
  repositoryCommit: string | null;
  configurationRevision: number | null;
  cleanupState: string;
  requestedAt: string | null;
  updatedAt: string | null;
  acceptedAt: string | null;
  lastHealthyAt: string | null;
};
export type StaffOperation = OwnerName & {
  intentId: string;
  applicationId: string;
  applicationSlug: string;
  ownerId: string;
  kind: string;
  state: string;
  stage: string;
  cleanupState: string;
  createdAt: string | null;
  updatedAt: string | null;
  statusObservedAt: string | null;
  attention: string;
  controllerErrorCode: string | null;
  guidance: string | null;
};

type Check = (v: unknown) => boolean;
const text =
  (max: number): Check =>
  (v) =>
    typeof v === 'string' && v.length <= max && !/[\u0000-\u001f\u007f]/.test(v);
const id: Check = (v) =>
  typeof v === 'string' && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(v);
const integer: Check = (v) => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0;
const bool: Check = (v) => typeof v === 'boolean';
const nullable =
  (check: Check): Check =>
  (v) =>
    v === null || check(v);
const stamp: Check = nullable(
  (v) => typeof v === 'string' && v.length <= 40 && Number.isFinite(Date.parse(v)),
);
const sha: Check = nullable((v) => typeof v === 'string' && /^[a-f0-9]{40}$/.test(v));
const state =
  (...values: string[]): Check =>
  (v) =>
    typeof v === 'string' && values.includes(v);
const url: Check = nullable((v) => {
  if (!text(512)(v)) return false;
  try {
    const u = new URL(v as string);
    return (
      u.protocol === 'https:' && !!u.hostname && !u.username && !u.password && !u.search && !u.hash
    );
  } catch {
    return false;
  }
});
function shape<T>(value: unknown, checks: Record<string, Check>): T {
  const data = record(value);
  if (
    Object.keys(data).length !== Object.keys(checks).length ||
    Object.entries(checks).some(([key, check]) => !check(data[key]))
  )
    throw new Error('Invalid staff metadata response');
  return data as T;
}
const ownerChecks = {
  ownerId: id,
  username: text(32),
  displayName: text(256),
  portalEnabled: bool,
  role: state('owner', 'staff', 'admin'),
};
const ownerName = { ownerUsername: text(32), ownerDisplayName: text(256) };
const owner = (v: unknown) => shape<StaffOwner>(v, ownerChecks);
const catalogChecks = {
  applicationId: id,
  ownerId: id,
  slug: text(40),
  lifecycleState: state('creating', 'ready', 'rejected', 'unknown'),
  savedRevision: integer,
  createdAt: stamp,
  repository: url,
};
const catalog = (v: unknown) => shape<StaffAppRow>(v, { ...catalogChecks, ...ownerName });
const health = state('healthy', 'unhealthy', 'stopped', 'unknown');
const app = (v: unknown) =>
  shape<StaffApp>(v, {
    ...catalogChecks,
    url,
    desiredRunning: bool,
    activeDeploymentId: nullable(id),
    acceptedDeployment: nullable((v) => {
      shape(v, { deploymentId: id, sourceCommit: sha, acceptedAt: stamp });
      return true;
    }),
    health: (v) => {
      shape(v, { process: health, route: health });
      return true;
    },
    observedAt: stamp,
    stale: bool,
  });
const cleanup = state('confirmed', 'not_required', 'pending', 'unknown');
const deployment = (v: unknown) =>
  shape<StaffDeployment>(v, {
    deploymentId: id,
    applicationId: id,
    status: state(
      'queued',
      'building',
      'deploying',
      'succeeded',
      'failed',
      'recovery_required',
      'unknown',
    ),
    repositoryCommit: sha,
    configurationRevision: nullable(integer),
    cleanupState: cleanup,
    requestedAt: stamp,
    updatedAt: stamp,
    acceptedAt: stamp,
    lastHealthyAt: stamp,
  });
const operation = (v: unknown) =>
  shape<StaffOperation>(v, {
    intentId: id,
    applicationId: id,
    applicationSlug: text(40),
    ownerId: id,
    ...ownerName,
    kind: state(
      'create_app',
      'save_configuration',
      'deploy',
      'adopt_app',
      'env_set',
      'env_delete',
      'storage_create',
      'storage_verify',
      'storage_rotate',
      'storage_delete',
      'app_enable',
      'app_disable',
      'unknown',
    ),
    state: state('prepared', 'unknown', 'accepted', 'succeeded', 'failed', 'blocked'),
    stage: state('queued', 'building', 'deploying', 'verifying', 'settled', 'recovery', 'unknown'),
    cleanupState: cleanup,
    createdAt: stamp,
    updatedAt: stamp,
    statusObservedAt: stamp,
    attention: state('none', 'awaiting_controller', 'failed', 'recovery_required'),
    guidance: nullable(text(512)),
    controllerErrorCode: nullable((v) => typeof v === 'string' && /^[A-Z][A-Z0-9_]{0,63}$/.test(v)),
  });
function quota(v: unknown): boolean {
  shape(
    v,
    Object.fromEntries(
      ['apps', 'concurrentOperations'].map((key) => [
        key,
        (v: unknown) => {
          shape(v, { limit: nullable(integer), used: integer, reserved: integer });
          return true;
        },
      ]),
    ),
  );
  return true;
}
function page<T>(v: unknown, decode: (v: unknown) => T): Page<T> {
  const data = pageData(v, decode);
  if (data.items.length > 50 || (data.nextCursor !== null && !id(data.nextCursor)))
    throw new Error('Invalid staff page');
  return data;
}
function query(values: Record<string, string | undefined>) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value) params.set(key, value);
  return params.size ? `?${params}` : '';
}
export const staffApi = {
  owners: (cursor?: string, signal?: AbortSignal, role?: StaffOwner['role']) =>
    request(
      '/staff/owners' + query({ role, cursor }),
      (v) => page(v, owner),
      undefined,
      false,
      signal,
    ),
  owner: (ownerId: string, signal?: AbortSignal) =>
    request(
      `/staff/owners/${ownerId}`,
      (v) => shape<StaffOwner & { quota: Quota }>(v, { ...ownerChecks, quota }),
      undefined,
      false,
      signal,
    ),
  apps: (ownerId?: string, cursor?: string, signal?: AbortSignal) =>
    request(
      '/staff/apps' + query({ ownerId, cursor }),
      (v) => page(v, catalog),
      undefined,
      false,
      signal,
    ),
  app: (appId: string, signal?: AbortSignal) =>
    request(`/staff/apps/${appId}`, app, undefined, false, signal),
  deployments: (appId: string, cursor?: string, signal?: AbortSignal) =>
    request(
      `/staff/apps/${appId}/deployments` + query({ cursor }),
      (v) => page(v, deployment),
      undefined,
      false,
      signal,
    ),
  deployment: (appId: string, deploymentId: string, signal?: AbortSignal) =>
    request(
      `/staff/apps/${appId}/deployments/${deploymentId}`,
      deployment,
      undefined,
      false,
      signal,
    ),
  operations: (ownerId?: string, applicationId?: string, cursor?: string, signal?: AbortSignal) =>
    request(
      '/staff/operations' + query({ ownerId, applicationId, cursor }),
      (v) => page(v, operation),
      undefined,
      false,
      signal,
    ),
};
