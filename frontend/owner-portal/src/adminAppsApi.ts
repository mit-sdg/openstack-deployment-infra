import { fields, pageData, record, request, resourceApi, type AppRecord, type Intent } from './api';
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
  adopt: (
    applicationId: string,
    ownerId: string | undefined,
    key: string,
    identityProviderConfirmed = false,
  ) =>
    request('/admin-apps/adopt', (v) => fields(v, { applicationId: 'string' }), {
      method: 'POST',
      body: { applicationId, ...(ownerId ? { ownerId } : {}), identityProviderConfirmed },
      key,
    }),
  resources: (confirm?: () => boolean) => resourceApi('/admin-apps', confirm),
  deploy: (
    id: string,
    configurationRevision: number,
    commit: string,
    maintenance: boolean,
    plan: unknown,
    identityProviderConfirmed: boolean,
    key: string,
  ) =>
    request(`/admin-apps/${id}/deployments`, intent, {
      method: 'POST',
      body: {
        configurationRevision,
        commit,
        maintenance,
        ...(plan ? { plan } : {}),
        identityProviderConfirmed,
      },
      key,
    }),
  reassign: (
    id: string,
    expectedOwnerId: string,
    ownerId: string,
    identityProviderConfirmed = false,
  ) =>
    request(`/admin-apps/${id}/owner`, record, {
      method: 'PUT',
      body: { ownerId, expectedOwnerId, identityProviderConfirmed },
    }),
  state: (id: string, desiredRunning: boolean, identityProviderConfirmed: boolean, key: string) =>
    request(`/admin-apps/${id}/state`, intent, {
      method: 'POST',
      body: { desiredRunning, identityProviderConfirmed },
      key,
    }),
  deleteStorage: (
    id: string,
    resource: string,
    confirmation: string,
    identityProviderConfirmed: boolean,
    key: string,
  ) =>
    request(`/admin-apps/${id}/storage/${resource}`, intent, {
      method: 'DELETE',
      body: { confirmation, identityProviderConfirmed },
      key,
    }),
};
