import { QueryClient } from '@tanstack/react-query';
import { ApiError, NetworkError } from './api';

const RETRY_DELAYS = [250, 750, 2000] as const;

export function retryQuery(failureCount: number, error: unknown) {
  if (failureCount >= RETRY_DELAYS.length) return false;
  if (error instanceof NetworkError) return true;
  return (
    error instanceof ApiError &&
    ([502, 503, 504].includes(error.status) ||
      (error.status === 429 && error.code === 'RATE_LIMITED' && error.retryable))
  );
}

export function queryRetryDelay(attempt: number, error: unknown) {
  const backoff = RETRY_DELAYS[Math.min(attempt, RETRY_DELAYS.length - 1)];
  return Math.max(backoff, error instanceof ApiError ? error.retryAfterSeconds * 1000 : 0);
}

export function createPortalQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: retryQuery,
        retryDelay: queryRetryDelay,
        refetchOnWindowFocus: true,
        staleTime: 1000,
      },
      mutations: { retry: false },
    },
  });
}
