import { useQuery } from '@tanstack/react-query';
import type { AppRecord, Page, Session } from '../api';
/** Shared app pages keep the personal/class destination selected consistently. */
export function useAppDestination(id?: string) {
  const app = useQuery<AppRecord>({ queryKey: ['app', id], enabled: false });
  const session = useQuery<Session>({ queryKey: ['session'], enabled: false });
  const personal = useQuery<Page<AppRecord>>({ queryKey: ['apps'], enabled: false });
  const mine =
    app.data?.ownerId === session.data?.user.id ||
    personal.data?.items.some((item) => item.applicationId === id);
  return app.data?.access === 'admin' && !mine ? '/all-apps' : '/apps';
}
