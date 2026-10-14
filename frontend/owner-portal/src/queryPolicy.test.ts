import { MutationObserver } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, NetworkError, api, clearCredentials, request } from './api';
import { createPortalQueryClient, queryRetryDelay, retryQuery } from './queryPolicy';

const limited = new ApiError(429, 'RATE_LIMITED', 'Wait briefly.', 0, true);
afterEach(() => {
  vi.useRealTimers();
  clearCredentials();
});

describe('query retries', () => {
  it.each([
    limited,
    new ApiError(502, 'UNAVAILABLE', 'Bad gateway.'),
    new ApiError(503, 'STATE_UNAVAILABLE', 'Unavailable.'),
    new ApiError(504, 'UNAVAILABLE', 'Gateway timeout.'),
    new NetworkError('Connection lost.'),
  ])('retries a transient error at most three times: %s', (error) => {
    for (const attempt of [0, 1, 2]) expect(retryQuery(attempt, error)).toBe(true);
    expect(retryQuery(3, error)).toBe(false);
  });

  it.each([400, 401, 403, 404, 409])(
    'never retries HTTP %s, even if marked retryable',
    (status) => {
      expect(retryQuery(0, new ApiError(status, 'REJECTED', 'Rejected.', 0, true))).toBe(false);
    },
  );

  it('does not retry non-retryable throttles, cancellations, or invalid data', () => {
    expect(retryQuery(0, new ApiError(429, 'RATE_LIMITED', 'Wait.'))).toBe(false);
    expect(retryQuery(0, new ApiError(429, 'OTHER_LIMIT', 'Wait.', 0, true))).toBe(false);
    expect(retryQuery(0, new DOMException('Cancelled', 'AbortError'))).toBe(false);
    expect(retryQuery(0, new Error('Invalid service response'))).toBe(false);
    expect(retryQuery(0, new TypeError('Programming error'))).toBe(false);
  });

  it('uses short backoff and waits at least as long as the server asks', () => {
    expect([0, 1, 2].map((attempt) => queryRetryDelay(attempt, limited))).toEqual([250, 750, 2000]);
    expect(queryRetryDelay(0, new ApiError(503, 'UNAVAILABLE', 'Wait.', 5))).toBe(5000);
    expect(queryRetryDelay(2, new ApiError(503, 'UNAVAILABLE', 'Wait.', 0.5))).toBe(2000);
  });

  it('recovers from 429, 503 and a fetch failure using the production query defaults', async () => {
    vi.useFakeTimers();
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ error: { code: 'RATE_LIMITED', retryable: true } }), {
          status: 429,
        }),
      )
      .mockResolvedValueOnce(new Response('<h1>Unavailable</h1>', { status: 503 }))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: 'loaded' })));
    vi.stubGlobal('fetch', fetch);
    const client = createPortalQueryClient();
    const loaded = client.fetchQuery({
      queryKey: ['read'],
      queryFn: () => request('/health', String),
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(fetch).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(249);
    expect(fetch).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(fetch).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(750);
    expect(fetch).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(2000);
    await expect(loaded).resolves.toBe('loaded');
    expect(fetch).toHaveBeenCalledTimes(4);
    client.clear();
  });

  it('stops a persistently unavailable query after the fourth request', async () => {
    vi.useFakeTimers();
    const read = vi.fn().mockRejectedValue(new NetworkError('Connection lost.'));
    const client = createPortalQueryClient();
    const failed = expect(client.fetchQuery({ queryKey: ['read'], queryFn: read })).rejects.toThrow(
      'Connection lost.',
    );
    await vi.advanceTimersByTimeAsync(3000);
    await failed;
    expect(read).toHaveBeenCalledTimes(4);
    client.clear();
  });

  it('never retries mutations with the production defaults', async () => {
    const client = createPortalQueryClient();
    const mutate = vi.fn().mockRejectedValue(limited);
    const observer = new MutationObserver(client, { mutationFn: mutate });
    await expect(observer.mutate(undefined)).rejects.toBe(limited);
    expect(mutate).toHaveBeenCalledOnce();
    client.clear();
  });
});

describe('HTTP retry hints and network errors', () => {
  it.each([502, 503, 504])('preserves HTTP %s for an HTML gateway response', async (status) => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response('<h1>Gateway error</h1>', { status })),
    );
    await expect(request('/health', String)).rejects.toMatchObject({ status });
  });

  it.each(['5', 'Wed, 14 Oct 2026 12:00:05 GMT'])('respects Retry-After: %s', async (header) => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-14T12:00:00Z'));
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: { code: 'RATE_LIMITED', retryable: true, retryAfterSeconds: 1 },
          }),
          {
            status: 429,
            headers: { 'Retry-After': header },
          },
        ),
      ),
    );
    await expect(request('/health', String)).rejects.toMatchObject({
      retryAfterSeconds: 5,
      retryable: true,
    });
  });

  it('uses the JSON retry hint when Retry-After is invalid', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ error: { retryAfterSeconds: 3 } }), {
          status: 503,
          headers: { 'Retry-After': 'invalid' },
        }),
      ),
    );
    await expect(request('/health', String)).rejects.toMatchObject({ retryAfterSeconds: 3 });
  });

  it('classifies fetch failures as retryable network errors', async () => {
    await expect(request('/health', String)).rejects.toBeInstanceOf(NetworkError);
  });

  it('retries a network failure while reading a successful response body', async () => {
    const response = new Response();
    vi.spyOn(response, 'json').mockRejectedValue(new TypeError('Connection lost'));
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response));
    await expect(request('/health', String)).rejects.toBeInstanceOf(NetworkError);
  });

  it('allows a page-sized staff burst while keeping the browser queue bounded', async () => {
    const fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          data: {
            csrfToken: 'csrf',
            expiresAt: '2099-01-01T00:00:00Z',
            platformName: 'Example',
            role: 'staff',
            user: { id: 'staff', username: 'taylor', displayName: 'Taylor' },
          },
        }),
      ),
    );
    vi.stubGlobal('fetch', fetch);
    await api.session();
    const releases: (() => void)[] = [];
    fetch.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          releases.push(() => resolve(new Response(JSON.stringify({ data: 'loaded' }))));
        }),
    );
    const reads = Array.from({ length: 13 }, () => request('/apps/app/configuration', String));
    await vi.waitFor(() => expect(releases).toHaveLength(12));
    releases[0]();
    await vi.waitFor(() => expect(releases).toHaveLength(13));
    for (const release of releases.slice(1)) release();
    await expect(Promise.all(reads)).resolves.toEqual(Array(13).fill('loaded'));
  });
});
