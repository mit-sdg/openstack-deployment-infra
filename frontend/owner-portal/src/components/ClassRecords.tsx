import {
  Button,
  Cluster,
  EmptyState,
  Icon,
  LoadingRows,
  buttonClass,
} from '@openstack-platform/ui';
import type { UseQueryResult } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { Link } from 'wouter';
import type { Page, Intent } from '../api';
import type { ClassActivity } from '../classApi';
import { Operation } from './Operation';
import { QueryError } from './Feedback';
export function pager(
  page: Page<unknown> | undefined,
  cursor: string | undefined,
  setCursor: (v?: string) => void,
) {
  if (!cursor && !page?.nextCursor) return null;
  return (
    <Cluster justify="end">
      {cursor && (
        <Button size="sm" variant="ghost" onClick={() => setCursor(undefined)}>
          First page
        </Button>
      )}
      {page?.nextCursor && (
        <Button size="sm" onClick={() => setCursor(page.nextCursor!)}>
          Next page
          <Icon name="chevron-right" />
        </Button>
      )}
    </Cluster>
  );
}
export function Loaded<T>({
  query,
  what,
  rows,
  children,
}: {
  query: UseQueryResult<T>;
  what: string;
  rows?: number;
  children: (data: T) => ReactNode;
}) {
  if (query.isPending) return <LoadingRows rows={rows} />;
  if (query.error)
    return (
      <div className="ui-section__body">
        <QueryError query={query} what={what} />
      </div>
    );
  return <>{children(query.data)}</>;
}
export function ActivityItem({
  item,
  showApp = true,
  showOwner = true,
}: {
  item: ClassActivity;
  showApp?: boolean;
  showOwner?: boolean;
}) {
  const intent: Intent = {
    intentId: item.intentId,
    appId: item.applicationId,
    appSlug: item.applicationSlug,
    kind: item.kind,
    state: item.state,
    createdAt: item.createdAt ?? '',
    operationId: item.operationId,
    operation: null,
    commit: null,
    safeError: item.guidance,
    controllerErrorCode: item.controllerErrorCode,
    canResume: item.canResume,
    requiresResubmit: item.requiresResubmit,
    actor: showOwner ? { displayName: item.ownerDisplayName, you: false } : undefined,
  };
  return <Operation intent={intent} showApp={showApp} showActor={showOwner} />;
}
export function ActivityEmpty({ filtered = false }: { filtered?: boolean }) {
  return (
    <EmptyState title="No activity yet">
      {filtered
        ? 'Nothing matches this filter yet.'
        : 'New apps, saved settings and deployments will appear here.'}
    </EmptyState>
  );
}
export function viewAll(href: string, more: boolean) {
  return more ? (
    <Link href={href} className={buttonClass({ variant: 'ghost', size: 'sm' })}>
      View all
      <Icon name="chevron-right" />
    </Link>
  ) : undefined;
}
