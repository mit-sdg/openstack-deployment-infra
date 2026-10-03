import { EmptyState, ErrorAlert, LoadingRows, PageSkeleton } from '@openstack-platform/ui';
import type { ReactNode } from 'react';

// Portal-wide feedback. New code can also import these from @openstack-platform/ui.
export { ErrorAlert as ErrorNotice, EmptyState, PageSkeleton };

/** Compatibility wrapper for pages that predate EmptyState. */
export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return <EmptyState title={title}>{children}</EmptyState>;
}

/** Loading rows for a section or list. Use PageSkeleton for whole pages. */
export function Loading() {
  return <LoadingRows />;
}
