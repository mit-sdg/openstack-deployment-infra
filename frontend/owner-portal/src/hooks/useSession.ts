import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { useLocation } from 'wouter';
import { api, clearCredentials } from '../api';

export function useSession() {
  const [location, navigate] = useLocation();
  const client = useQueryClient();
  const signIn = location === '/sign-in';
  const session = useQuery({
    queryKey: ['session'],
    queryFn: api.session,
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
      navigate('/sign-in');
      client.clear();
    }
    window.addEventListener('portal-session-ended', ended);
    return () => window.removeEventListener('portal-session-ended', ended);
  }, [client, navigate]);
  useEffect(() => {
    document.title = 'My applications · Owner portal';
    document.getElementById('main')?.focus();
  }, [location]);
  return { signIn, session, logout };
}
