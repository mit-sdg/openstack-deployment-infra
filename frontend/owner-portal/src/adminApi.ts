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
  /** Null for admin accounts, which have no limits. */
  appLimit: number | null;
  concurrencyLimit: number | null;
};
export type AdminAudit = {
  id: number;
  actorId: string | null;
  targetId: string | null;
  action: string;
  details: Record<string, unknown>;
  createdAt: string;
  actorUsername: string | null;
  actorDisplayName: string | null;
  targetUsername: string | null;
  targetDisplayName: string | null;
};
export const adminApi = {
  account: (id: string) =>
    request(`/people/${id}/account`, (v) => {
      const account = fields(v, {
        userId: 'string',
        username: 'string',
        displayName: 'string',
        role: 'string',
        enabled: 'boolean',
        status: 'string',
        method: 'string',
        appCount: 'number',
        totpEnabled: 'boolean',
      });
      for (const key of ['appLimit', 'concurrencyLimit'])
        if (account[key] !== null && typeof account[key] !== 'number')
          throw new Error('Invalid service response');
      return account as Account;
    }),
  create: (username: string, displayName: string, role: string) =>
    request(
      '/people',
      (v) =>
        fields(v, { userId: 'string', setupUrl: 'string' }) as { userId: string; setupUrl: string },
      { method: 'POST', body: { username, displayName, role } },
    ),
  change: (userId: string, action: string, value: unknown = null) =>
    request(
      `/people/${userId}/account`,
      (v) => record(v) as { userId: string; setupUrl?: string },
      {
        method: 'PATCH',
        body: { action, value },
      },
    ),
  quotas: (userId: string, apps: number, concurrentOperations: number) =>
    request(`/people/${userId}/quotas`, (v) => v as Quota, {
      method: 'PUT',
      body: { apps, concurrentOperations },
    }),
  reauthenticate: (password: string, totp: string) =>
    request('/reauthenticate', (v) => fields(v, { stepUpExpiresAt: 'string' }), {
      method: 'POST',
      body: { password, totp },
    }),
  audit: (cursor?: string) =>
    request('/audit' + (cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''), (v) =>
      pageData(
        v,
        (item) =>
          fields(item, { id: 'number', action: 'string', createdAt: 'string' }) as AdminAudit,
      ),
    ),
};
