import {
  fields,
  intentData,
  pageData,
  record,
  request,
  resourceApi,
  runtimeLogData,
  type AppRecord,
  type Intent,
  type LogStream,
} from './api';
export type AppOwner = {
  userId: string;
  username: string;
  displayName: string;
  role: 'owner' | 'staff' | 'admin';
  enabled: boolean;
  status: 'active' | 'pending';
};
export type ManagedApp = AppRecord & {
  ownerId: string;
  ownerUsername: string;
  ownerDisplayName: string;
  identityProvider: boolean;
  requiresMaintenance: boolean;
  sizing: { workerFlavor: string; cpuMHz: number; memoryMiB: number } | null;
};
export type CatalogApp = Pick<
  ManagedApp,
  | 'applicationId'
  | 'slug'
  | 'ownerId'
  | 'ownerUsername'
  | 'ownerDisplayName'
  | 'savedRevision'
  | 'lifecycleState'
> & {
  /** From the last observation; null until the app has been observed. */
  url: string | null;
  lastDeployedAt: string | null;
};
const nullableString = (value: unknown) => {
  if (value !== null && typeof value !== 'string') throw new Error('Invalid service response');
  return value;
};
const intent = (v: unknown) => fields(v, { intentId: 'string', state: 'string' }) as Intent;
export const adminAppsApi = {
  attention: (id: string) =>
    request(`/admin-apps/${id}/activity?attention=1`, (v) => {
      const data = record(v);
      if (!Array.isArray(data.items)) throw new Error('Invalid service response');
      return data.items.map(intentData);
    }),
  owners: (q: string) =>
    request('/admin-apps/owners?' + new URLSearchParams({ q, limit: '6' }), (v) =>
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

  logs: (id: string, stream: LogStream) =>
    request(`/admin-apps/${id}/logs?stream=${stream}`, runtimeLogData),
  list: (cursor?: string) =>
    request('/admin-apps' + (cursor ? `?cursor=${cursor}` : ''), (v) =>
      pageData(v, (item) => {
        const data = fields(item, {
          applicationId: 'string',
          slug: 'string',
          ownerId: 'string',
          ownerUsername: 'string',
          ownerDisplayName: 'string',
          savedRevision: 'number',
          lifecycleState: 'string',
        });
        nullableString(data.url);
        nullableString(data.lastDeployedAt);
        return data as CatalogApp;
      }),
    ),
  detail: (id: string) =>
    request(
      `/admin-apps/${id}`,
      (v) =>
        fields(v, {
          applicationId: 'string',
          slug: 'string',
          ownerId: 'string',
          ownerUsername: 'string',
          ownerDisplayName: 'string',
          identityProvider: 'boolean',
          requiresMaintenance: 'boolean',
        }) as ManagedApp,
    ),
  create: (slug: string, ownerId: string, key: string) =>
    request('/admin-apps', (v) => record(record(v).app) as AppRecord, {
      method: 'POST',
      body: { slug, ownerId },
      key,
    }),
  adopt: (applicationId: string, ownerId: string | undefined, key: string) =>
    request('/admin-apps/adopt', (v) => fields(v, { applicationId: 'string' }), {
      method: 'POST',
      body: { applicationId, ...(ownerId ? { ownerId } : {}) },
      key,
    }),
  resources: () => resourceApi('/admin-apps'),
  deploy: (
    id: string,
    configurationRevision: number,
    commit: string,
    maintenance: boolean,
    plan: unknown,
    key: string,
  ) =>
    request(`/admin-apps/${id}/deployments`, intent, {
      method: 'POST',
      body: {
        configurationRevision,
        commit,
        maintenance,
        ...(plan ? { plan } : {}),
      },
      key,
    }),
  reassign: (id: string, expectedOwnerId: string, ownerId: string) =>
    request(`/admin-apps/${id}/owner`, record, {
      method: 'PUT',
      body: { ownerId, expectedOwnerId },
    }),
  state: (id: string, desiredRunning: boolean, key: string) =>
    request(`/admin-apps/${id}/state`, intent, {
      method: 'POST',
      body: { desiredRunning },
      key,
    }),
  deleteStorage: (id: string, resource: string, confirmation: string, key: string) =>
    request(`/admin-apps/${id}/storage/${resource}`, intent, {
      method: 'DELETE',
      body: { confirmation },
      key,
    }),
};
