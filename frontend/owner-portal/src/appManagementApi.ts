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
  attention: Intent[];
  url: string | null;
  lastDeployedAt: string | null;
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
  deleteStorage: (id: string, resource: string, confirmation: string, key: string) =>
    request(`/apps/${id}/storage/${resource}`, intentData, {
      method: 'DELETE',
      body: { confirmation },
      key,
    }),
};
