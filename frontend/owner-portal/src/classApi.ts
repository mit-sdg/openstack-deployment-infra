import { pageData, record, request, type Page, type Quota } from './api';
export type Person = {
  ownerId: string;
  username: string;
  displayName: string;
  portalEnabled: boolean;
  role: 'owner' | 'staff' | 'admin';
  status: 'active' | 'pending';
};
export type ClassActivity = {
  intentId: string;
  applicationId: string;
  applicationSlug: string;
  ownerId: string;
  ownerUsername: string;
  ownerDisplayName: string;
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
  operationId: string | null;
  canResume: boolean;
  requiresResubmit: boolean;
};
const id = (v: unknown) =>
  typeof v === 'string' && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(v);
const text = (v: unknown) =>
  typeof v === 'string' && v.length <= 256 && !/[\u0000-\u001f\u007f]/.test(v);
const bounded = (maximum: number) => (v: unknown) =>
  typeof v === 'string' && v.length <= maximum && !/[\u0000-\u001f\u007f]/.test(v);
const state =
  (...values: string[]) =>
  (v: unknown) =>
    typeof v === 'string' && values.includes(v);
const stamp = (v: unknown) =>
  v === null || (typeof v === 'string' && v.length <= 40 && Number.isFinite(Date.parse(v)));
const integer = (v: unknown) => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0;
function shape<T>(v: unknown, checks: Record<string, (v: unknown) => boolean>): T {
  const data = record(v);
  if (
    Object.keys(data).length !== Object.keys(checks).length ||
    Object.entries(checks).some(([key, check]) => !check(data[key]))
  )
    throw new Error('Invalid class metadata response');
  return data as T;
}
const personChecks = {
  ownerId: id,
  username: bounded(32),
  displayName: text,
  portalEnabled: (v: unknown) => typeof v === 'boolean',
  role: state('owner', 'staff', 'admin'),
  status: state('active', 'pending'),
};
const person = (v: unknown) => shape<Person>(v, personChecks);
const quota = (v: unknown) => {
  shape(v, {
    apps: (value) => {
      shape(value, { limit: (v) => v === null || integer(v), used: integer, reserved: integer });
      return true;
    },
    concurrentOperations: (value) => {
      shape(value, { limit: (v) => v === null || integer(v), used: integer, reserved: integer });
      return true;
    },
  });
  return true;
};
const activity = (v: unknown) =>
  shape<ClassActivity>(v, {
    intentId: id,
    applicationId: id,
    applicationSlug: bounded(40),
    ownerId: id,
    ownerUsername: bounded(32),
    ownerDisplayName: text,
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
      'app_restart',
      'unknown',
    ),
    state: state('prepared', 'unknown', 'accepted', 'succeeded', 'failed', 'blocked'),
    stage: state('queued', 'building', 'deploying', 'verifying', 'settled', 'recovery', 'unknown'),
    cleanupState: state('confirmed', 'not_required', 'pending', 'unknown'),
    createdAt: stamp,
    updatedAt: stamp,
    statusObservedAt: stamp,
    attention: state('none', 'awaiting_controller', 'failed', 'recovery_required'),
    controllerErrorCode: (v) =>
      v === null || (typeof v === 'string' && /^[A-Z][A-Z0-9_]{0,63}$/.test(v)),
    guidance: (v) => v === null || bounded(512)(v),
    operationId: (v) => v === null || id(v),
    canResume: (v) => typeof v === 'boolean',
    requiresResubmit: (v) => typeof v === 'boolean',
  });
function query(params: Record<string, string | undefined>) {
  const values = Object.entries(params).filter((entry): entry is [string, string] => !!entry[1]);
  return '?' + new URLSearchParams(values);
}
export const classApi = {
  people: (cursor?: string, signal?: AbortSignal, q?: string) =>
    request('/people' + query({ cursor, q }), (v) => pageData(v, person), undefined, false, signal),
  person: (id: string, signal?: AbortSignal) =>
    request(
      `/people/${id}`,
      (v) => shape<Person & { quota: Quota }>(v, { ...personChecks, quota }),
      undefined,
      false,
      signal,
    ),
  activity: (
    ownerId?: string,
    applicationId?: string,
    cursor?: string,
    signal?: AbortSignal,
    attention = false,
  ) =>
    request(
      '/activity' +
        query({ ownerId, applicationId, cursor, attention: attention ? '1' : undefined }),
      (v) => pageData(v, activity),
      undefined,
      false,
      signal,
    ),
};
export type ActivityPage = Page<ClassActivity>;
