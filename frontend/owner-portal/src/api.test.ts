import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api, clearCredentials, record, request } from './api';
import { createPortalQueryClient } from './queryPolicy';

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
const rejected = () => json(403, { error: { code: 'CSRF_REJECTED' } });
const session = () =>
  json(200, {
    data: {
      csrfToken: 'replacement',
      platformName: 'Example platform',
      role: 'staff',
      expiresAt: '2030-01-01T00:00:00Z',
      user: { id: 'u', displayName: 'Staff', username: 'staff' },
    },
  });

describe('CSRF recovery', () => {
  beforeEach(clearCredentials);

  it.each(['GET', 'POST'])(
    'refreshes and retries a %s once with the same inputs',
    async (method) => {
      const fetch = vi
        .fn()
        .mockResolvedValueOnce(rejected())
        .mockResolvedValueOnce(session())
        .mockResolvedValueOnce(json(200, { data: { ready: true } }));
      vi.stubGlobal('fetch', fetch);
      const signal = new AbortController().signal;
      const options =
        method === 'POST' ? { method, body: { flavor: 'small' }, key: 'fixed-key' } : undefined;
      await expect(
        request('/apps/app/builder-size', record, options, false, signal),
      ).resolves.toEqual({ ready: true });
      expect(fetch.mock.calls.map(([path]) => path)).toEqual([
        '/api/v1/apps/app/builder-size',
        '/api/v1/session',
        '/api/v1/apps/app/builder-size',
      ]);
      const original = fetch.mock.calls[0][1];
      const retried = fetch.mock.calls[2][1];
      expect(retried).toEqual({
        ...original,
        headers: { ...original.headers, 'X-CSRF-Token': 'replacement' },
      });
      expect(retried.signal).toBe(signal);
      if (options) {
        expect(retried.headers['Idempotency-Key']).toBe('fixed-key');
        expect(retried.body).toBe(JSON.stringify(options.body));
      }
    },
  );

  it.each(['GET', 'POST'])('stops after a second CSRF rejection for %s', async (method) => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(rejected())
      .mockResolvedValueOnce(session())
      .mockResolvedValueOnce(rejected());
    vi.stubGlobal('fetch', fetch);
    const options = method === 'POST' ? { method, body: {}, key: 'fixed-key' } : undefined;
    await expect(request('/apps/app/builder-size', record, options)).rejects.toMatchObject({
      status: 403,
      code: 'CSRF_REJECTED',
    });
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it('shares one session refresh across simultaneous CSRF rejections', async () => {
    let refreshed = false;
    const fetch = vi.fn(async (path: string) => {
      if (path === '/api/v1/session') {
        refreshed = true;
        return session();
      }
      return refreshed ? json(200, { data: { ready: true } }) : rejected();
    });
    vi.stubGlobal('fetch', fetch);
    await expect(
      Promise.all([
        request('/people', record),
        request('/all-apps', record),
        request('/activity', record),
      ]),
    ).resolves.toEqual([{ ready: true }, { ready: true }, { ready: true }]);
    expect(fetch.mock.calls.filter(([path]) => path === '/api/v1/session')).toHaveLength(1);
    expect(fetch).toHaveBeenCalledTimes(7);
  });

  it('does not add query backoff retries after the one-time CSRF replay fails', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(rejected())
      .mockResolvedValueOnce(session())
      .mockResolvedValueOnce(rejected());
    vi.stubGlobal('fetch', fetch);
    const client = createPortalQueryClient();
    try {
      await expect(
        client.fetchQuery({ queryKey: ['people'], queryFn: () => request('/people', record) }),
      ).rejects.toMatchObject({ status: 403, code: 'CSRF_REJECTED' });
      expect(fetch).toHaveBeenCalledTimes(3);
    } finally {
      client.clear();
    }
  });

  it('does not refresh or retry other authorization failures', async () => {
    const fetch = vi.fn().mockResolvedValue(json(403, { error: { code: 'ACCESS_DENIED' } }));
    vi.stubGlobal('fetch', fetch);
    await expect(request('/people', record)).rejects.toMatchObject({ code: 'ACCESS_DENIED' });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('does not retry a mutation when the session refresh fails', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(rejected())
      .mockResolvedValueOnce(json(401, { error: { code: 'SESSION_EXPIRED' } }));
    vi.stubGlobal('fetch', fetch);
    await expect(
      request('/apps', record, { method: 'POST', body: {}, key: 'fixed-key' }),
    ).rejects.toMatchObject({ code: 'SESSION_EXPIRED' });
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('does not replay a mutation if credentials change during recovery', async () => {
    const refresh = vi.spyOn(api, 'session').mockImplementation(async () => {
      clearCredentials();
      return {} as Awaited<ReturnType<typeof api.session>>;
    });
    const fetch = vi.fn().mockResolvedValueOnce(rejected());
    vi.stubGlobal('fetch', fetch);
    await expect(
      request('/apps', record, { method: 'POST', body: {}, key: 'fixed-key' }),
    ).rejects.toMatchObject({ code: 'SESSION_EXPIRED' });
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
