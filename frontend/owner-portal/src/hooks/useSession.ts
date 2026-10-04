import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { useLocation } from 'wouter';
import { api, clearCredentials } from '../api';

export function useSession() {
  const [location, navigate] = useLocation();
  const client = useQueryClient();
  const signIn = ['/sign-in', '/signin', '/setup', '/activate'].includes(location);
  const session = useQuery({
    queryKey: ['session'],
    queryFn: ({ signal }) => api.session(signal),
    enabled: !signIn,
    retry: false,
  });
  const logout = useMutation({
    mutationFn: api.logout,
    onSuccess: () => {
      clearCredentials();
      navigate('/sign-in');
      client.clear();
    },
  });
  useEffect(() => {
    function ended() {
      clearCredentials();
      void client.cancelQueries();
      navigate('/sign-in');
      client.clear();
    }
    window.addEventListener('portal-session-ended', ended);
    return () => window.removeEventListener('portal-session-ended', ended);
  }, [client, navigate, session.data?.role]);
  useEffect(() => {
    if (signIn || !session.data) return;
    const expires = Date.parse(session.data.expiresAt);
    const timer = window.setTimeout(
      () => window.dispatchEvent(new Event('portal-session-ended')),
      Math.max(0, expires - Date.now()),
    );
    return () => window.clearTimeout(timer);
  }, [signIn, session.data?.expiresAt]);
  useEffect(() => {
    document.getElementById('main')?.focus();
  }, [location]);
  return { signIn, session, logout };
}
