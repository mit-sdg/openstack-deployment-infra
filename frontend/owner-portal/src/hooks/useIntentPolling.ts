import { useQuery } from '@tanstack/react-query';
import { api } from '../api';

export function useOwnerIntents() {
  return useQuery({
    queryKey: ['intents'],
    queryFn: api.intents,
    refetchInterval: (query) =>
      query.state.data?.items.some((i) => !['succeeded', 'failed', 'blocked'].includes(i.state))
        ? 1500
        : 10000,
  });
}
export function useIntentPolling(id: string | null) {
  return useQuery({
    queryKey: ['intent', id],
    queryFn: () => api.intent(id!),
    enabled: !!id,
    refetchInterval: (query) =>
      query.state.data &&
      (query.state.data.requiresResubmit ||
        ['succeeded', 'failed', 'blocked'].includes(query.state.data.state))
        ? false
        : 1000,
  });
}
