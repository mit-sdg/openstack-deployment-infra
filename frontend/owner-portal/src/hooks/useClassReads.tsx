import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { createContext, useContext, useEffect, useRef, useState } from 'react';
import { ApiError, type Session } from '../api';
export const ClassContext = createContext({ userId: '', active: true });

/** Pause class reads in hidden tabs; preserve staff/admin idle limits without shortening owner sessions. */
export function useActive(role?: Session['role']) {
  const [active, setActive] = useState(!document.hidden);
  useEffect(() => {
    let interacted = Date.now();
    function interaction() {
      interacted = Date.now();
    }
    function visibility() {
      setActive(!document.hidden);
    }
    for (const event of ['pointerdown', 'keydown', 'scroll'])
      window.addEventListener(event, interaction, { passive: true });
    document.addEventListener('visibilitychange', visibility);
    const timer = window.setInterval(() => {
      if (
        (role === 'staff' || role === 'admin') &&
        Date.now() - interacted >= (role === 'admin' ? 900000 : 600000)
      )
        window.dispatchEvent(new Event('portal-session-ended'));
    }, 1000);
    return () => {
      window.clearInterval(timer);
      for (const event of ['pointerdown', 'keydown', 'scroll'])
        window.removeEventListener(event, interaction);
      document.removeEventListener('visibilitychange', visibility);
    };
  }, [role]);
  return active;
}

/** Poll intervals: lists every 30 seconds, detail pages every 15. */
export const LIST = 30000;
export const DETAIL = 15000;

// The broker allows two active staff reads per account and one controller
// observation at a time. So each page has one head query that polls and
// refetches on focus; related reads start once the one before has settled
// (`enabled`) and refetch one after another when the head updates (useFollow).
export function useRead<T>(
  key: (string | undefined)[],
  read: (signal: AbortSignal) => Promise<T>,
  { poll = 0, enabled = true, staleTime = 0 } = {},
) {
  const { userId, active } = useContext(ClassContext);
  return useQuery({
    queryKey: ['class', userId, ...key],
    queryFn: ({ signal }) => read(signal),
    enabled: active && enabled,
    staleTime,
    retry: false,
    refetchOnWindowFocus: poll > 0,
    refetchInterval: (query) =>
      !active || !poll || query.state.errorUpdateCount >= 3
        ? false
        : query.state.error instanceof ApiError
          ? Math.max(poll, query.state.error.retryAfterSeconds * 1000)
          : poll,
    refetchIntervalInBackground: false,
  });
}

/** Refetches related sections one at a time after the page's head query updates. */
export function useFollow(head: UseQueryResult<unknown>, followers: UseQueryResult<unknown>[]) {
  const latest = useRef(followers);
  latest.current = followers;
  const seen = useRef(head.dataUpdatedAt);
  useEffect(() => {
    if (head.dataUpdatedAt === seen.current) return;
    seen.current = head.dataUpdatedAt;
    let cancelled = false;
    void (async () => {
      for (const query of latest.current) {
        if (cancelled) return;
        if (!query.isPending) await query.refetch();
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [head.dataUpdatedAt]);
}
