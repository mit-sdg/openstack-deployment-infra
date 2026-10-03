import { Loading as SharedLoading } from '@openstack-platform/ui';
export { Empty, ErrorNotice } from '@openstack-platform/ui';
export function Loading() {
  return <SharedLoading>Loading your application workspace…</SharedLoading>;
}
