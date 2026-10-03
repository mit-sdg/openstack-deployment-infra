import { fields, pageData, record, request, type Quota } from './api';
export type Account = {
  userId: string;
  username: string;
  displayName: string;
  role: 'owner' | 'staff' | 'admin';
  enabled: boolean;
  status: 'active' | 'pending';
  method: 'local' | 'commons';
  lastSignIn: string | null;
  appCount: number;
  totpEnabled: boolean;
  appLimit: number;
  concurrencyLimit: number;
};
export type AdminAudit = {
  id: number;
  actorId: string | null;
  targetId: string | null;
  action: string;
  details: Record<string, unknown>;
  createdAt: string;
};
export const adminApi = {
  accounts: (q = '', cursor?: string, limit?: number) =>
    request(
      '/accounts?' +
        new URLSearchParams({
          ...(q ? { q } : {}),
          ...(cursor ? { cursor } : {}),
          ...(limit ? { limit: String(limit) } : {}),
        }),
      (v) =>
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
              method: 'string',
              appCount: 'number',
              totpEnabled: 'boolean',
              appLimit: 'number',
              concurrencyLimit: 'number',
            }) as Account,
        ),
    ),
  create: (username: string, displayName: string, role: string) =>
    request(
      '/accounts',
      (v) =>
        fields(v, { userId: 'string', setupUrl: 'string' }) as { userId: string; setupUrl: string },
      { method: 'POST', body: { username, displayName, role } },
    ),
  change: (userId: string, action: string, value: unknown = null) =>
    request(`/accounts/${userId}`, (v) => record(v) as { userId: string; setupUrl?: string }, {
      method: 'PATCH',
      body: { action, value },
    }),
  quotas: (userId: string, apps: number, concurrentOperations: number) =>
    request(`/accounts/${userId}/quotas`, (v) => v as Quota, {
      method: 'PUT',
      body: { apps, concurrentOperations },
    }),
  reauthenticate: (password: string, totp: string) =>
    request('/reauthenticate', (v) => fields(v, { stepUpExpiresAt: 'string' }), {
      method: 'POST',
      body: { password, totp },
    }),
  audit: (cursor?: string) =>
    request('/account-audit' + (cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''), (v) =>
      pageData(
        v,
        (item) =>
          fields(item, { id: 'number', action: 'string', createdAt: 'string' }) as AdminAudit,
      ),
    ),
};
