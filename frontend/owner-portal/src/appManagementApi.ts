import { fields, intentData, pageData, record, request, type AppRecord, type Intent } from './api';
export type AppOwner = {
  userId: string;
  username: string;
  displayName: string;
  role: 'owner' | 'staff' | 'admin';
  enabled: boolean;
  status: 'active' | 'pending';
};
export type CatalogApp = {
  applicationId: string;
  slug: string;
  ownerId: string;
  ownerUsername: string;
  ownerDisplayName: string;
  lifecycleState: string;
  savedRevision: number;
  appState: string;
  observedAt: string | null;
  refreshing: boolean;
  attention: Intent[];
  url: string | null;
  lastDeployedAt: string | null;
};
/** A staff snapshot of one database; file details appear once it's ready. */
export type Snapshot = {
  snapshotId: string;
  resourceId: string;
  state: 'preparing' | 'ready' | 'failed' | 'expired';
  startedAt: string | null;
  filename: string | null;
  bytes: number | null;
  sha256: string | null;
  takenAt: string | null;
  expiresAt: string | null;
  error: string | null;
};
export type SnapshotLink = {
  url: string;
  expiresAt: string;
  filename: string;
  bytes: number;
  sha256: string;
  takenAt: string;
};
const snapshotStates = ['preparing', 'ready', 'failed', 'expired'];
const snapshotData = (v: unknown) => {
  const data = fields(v, { snapshotId: 'string', resourceId: 'string', state: 'string' });
  if (!snapshotStates.includes(data.state as string)) throw new Error('Invalid service response');
  if (data.state === 'ready')
    fields(v, { filename: 'string', bytes: 'number', sha256: 'string', takenAt: 'string' });
  return data as Snapshot;
};
const snapshotLinkData = (v: unknown) => {
  const data = fields(v, { url: 'string', expiresAt: 'string', filename: 'string' });
  if (!(data.url as string).startsWith('https://')) throw new Error('Invalid service response');
  return data as SnapshotLink;
};
export const appManagementApi = {
  owners: (q: string) =>
    request('/people/eligible-owners?' + new URLSearchParams({ q, limit: '6' }), (v) =>
      pageData(
        v,
        (item) =>
          fields(item, {
            userId: 'string',
            username: 'string',
            displayName: 'string',
            role: 'string',
            enabled: 'boolean',
            status: 'string',
          }) as AppOwner,
      ),
    ),
  list: (cursor?: string, q = '', ownerId?: string, status?: string) =>
    request(
      '/all-apps?' +
        new URLSearchParams({
          ...(cursor ? { cursor } : {}),
          ...(q ? { q } : {}),
          ...(ownerId ? { ownerId } : {}),
          ...(status ? { status } : {}),
        }),
      (v) =>
        pageData(v, (item) => {
          const data = fields(item, {
            applicationId: 'string',
            slug: 'string',
            ownerId: 'string',
            ownerUsername: 'string',
            ownerDisplayName: 'string',
            lifecycleState: 'string',
            savedRevision: 'number',
            appState: 'string',
            refreshing: 'boolean',
          });
          if (!Array.isArray(data.attention)) throw new Error('Invalid app response');
          return { ...data, attention: data.attention.map(intentData) } as CatalogApp;
        }),
    ),
  create: (slug: string, ownerId: string, key: string) =>
    request('/all-apps', (v) => record(record(v).app) as AppRecord, {
      method: 'POST',
      body: { slug, ownerId },
      key,
    }),
  adopt: (applicationId: string, ownerId: string | undefined, key: string) =>
    request('/all-apps/adopt', (v) => fields(v, { applicationId: 'string' }), {
      method: 'POST',
      body: { applicationId, ...(ownerId ? { ownerId } : {}) },
      key,
    }),
  reassign: (id: string, expectedOwnerId: string, ownerId: string) =>
    request(`/apps/${id}/owner`, record, { method: 'PUT', body: { ownerId, expectedOwnerId } }),
  deleteApp: (id: string, confirmation: string, key: string) =>
    request(`/apps/${id}/delete`, intentData, {
      method: 'POST',
      body: { confirmation },
      key,
    }),
  requestSnapshot: (id: string, resource: string, key: string) =>
    request(`/apps/${id}/storage/${resource}/snapshots`, snapshotData, {
      method: 'POST',
      body: {},
      key,
    }),
  snapshot: (id: string, resource: string, snapshot: string) =>
    request(`/apps/${id}/storage/${resource}/snapshots/${snapshot}`, snapshotData),
  /** A fresh download link, made at the moment someone downloads. */
  snapshotLink: (id: string, resource: string, snapshot: string) =>
    request(`/apps/${id}/storage/${resource}/snapshots/${snapshot}/link`, snapshotLinkData, {
      method: 'POST',
      body: {},
    }),
  deleteStorage: (id: string, resource: string, confirmation: string, key: string) =>
    request(`/apps/${id}/storage/${resource}`, intentData, {
      method: 'DELETE',
      body: { confirmation },
      key,
    }),
};
