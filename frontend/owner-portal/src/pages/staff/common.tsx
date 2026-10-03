import {
  Button,
  Cluster,
  DataTable,
  EmptyState,
  Icon,
  ListItem,
  LoadingRows,
  RelativeTime,
  Skeleton,
  backLinkClass,
  buttonClass,
  type Column,
} from '@openstack-platform/ui';
import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { Link, useLocation } from 'wouter';
import { ApiError, type Page } from '../../api';
import { QueryError } from '../../components/Feedback';
import { Status } from '../../components/Status';
import {
  staffApi,
  type StaffApp,
  type StaffAppRow,
  type StaffDeployment,
  type StaffOperation,
} from '../../staffApi';
import { activityTitle, short } from '../../utils/presentation';

export const StaffContext = createContext({ userId: '', active: true });

/** False while the tab is hidden; ends the session after 10 idle minutes. */
export function useActive() {
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
      if (Date.now() - interacted >= 600000)
        window.dispatchEvent(new Event('portal-session-ended'));
    }, 1000);
    return () => {
      window.clearInterval(timer);
      for (const event of ['pointerdown', 'keydown', 'scroll'])
        window.removeEventListener(event, interaction);
      document.removeEventListener('visibilitychange', visibility);
    };
  }, []);
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
  const { userId, active } = useContext(StaffContext);
  return useQuery({
    queryKey: ['staff', 'staff', userId, ...key],
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

const lookup = { staleTime: 60000 };
export function useOwner(id: string | undefined, enabled = true) {
  return useRead(['owner', id], (signal) => staffApi.owner(id!, signal), {
    ...lookup,
    enabled: !!id && enabled,
  });
}
/** The app record, read from cache when the app page loaded it recently. */
export function useCachedApp(id: string, enabled = true) {
  return useRead(['app', id], (signal) => staffApi.app(id, signal), { ...lookup, enabled });
}

/** Section footer for a paged list, or null when there is one page. */
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

/** A section body for a query: rows while loading, a retryable error on failure. */
export function Loaded<T>({
  query,
  what,
  rows,
  children,
}: {
  query: UseQueryResult<T>;
  /** What failed to load, e.g. "recent activity". */
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

/** True when a detail read failed because the record doesn't exist (any more). */
export function isMissing(error: unknown) {
  return error instanceof ApiError && [404, 410].includes(error.status);
}

/** Unknown or deleted owner, app or deployment: the same shape as an unknown route. */
export function Missing({
  title,
  href,
  action,
  children,
}: {
  title: string;
  href: string;
  action: string;
  children: ReactNode;
}) {
  return (
    <EmptyState
      title={title}
      icon="search"
      action={
        <Link href={href} className={buttonClass()}>
          {action}
        </Link>
      }
    >
      {children}
    </EmptyState>
  );
}

export function Back({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Link href={href} className={backLinkClass}>
      <Icon name="arrow-left" />
      {children}
    </Link>
  );
}

/**
 * Loading header for a detail page: the real back link, then placeholders
 * for the title, its badge and an optional line under it (candidate for a
 * `back` prop on PageHeaderSkeleton).
 */
export function DetailHeaderSkeleton({
  back,
  meta = false,
  extra = false,
}: {
  back: ReactNode;
  meta?: boolean;
  extra?: boolean;
}) {
  return (
    <div className="ui-page-header">
      <div className="ui-page-header__back">{back}</div>
      <div className="ui-page-header__row" aria-hidden="true">
        <div className="ui-page-header__title">
          <span className="ui-skeleton ui-skeleton--heading" />
          {meta && <span className="ui-skeleton ui-skeleton--badge" />}
        </div>
      </div>
      {extra && (
        <div className="ui-page-header__extra" aria-hidden="true">
          <Skeleton width="quarter" />
        </div>
      )}
    </div>
  );
}

export function OwnerLink({ id, name }: { id: string; name: string }) {
  return (
    <Link href={`/staff/owners/${id}`} className="ui-link">
      {name}
    </Link>
  );
}

export function AppLink({ id, name }: { id: string; name: string }) {
  return (
    <Link href={`/staff/apps/${id}`} className="ui-link">
      {name}
    </Link>
  );
}

/** Shows only a filter that is set, with a way to clear it. */
export function FilterChip({
  label,
  value,
  clear,
}: {
  label: string;
  value: ReactNode;
  clear: string;
}) {
  return (
    <Cluster gap={2} className="ui-text-sm">
      <span className="ui-text-muted">{label}</span>
      {value}
      <Link href={clear} className={buttonClass({ variant: 'ghost', size: 'sm' })}>
        <Icon name="x" />
        Clear filter
      </Link>
    </Cluster>
  );
}

/**
 * The app page's one status word: lifecycle first, then health from the
 * latest check. Lists only know the lifecycle, so they show a status only
 * while an app is being created or when it wasn't created.
 */
export function appState(app: StaffApp) {
  if (app.lifecycleState !== 'ready') return app.lifecycleState;
  const { process, route } = app.health;
  if (app.stale) return 'unknown';
  if (!app.desiredRunning || process === 'stopped') return 'stopped';
  if (process === 'healthy' && route === 'healthy') return 'healthy';
  if (process === 'unhealthy' || route === 'unhealthy') return 'unhealthy';
  return 'unknown';
}

/** App table columns. Without the owner column, phones show two-line rows. */
export function appColumns(showOwner: boolean): Column<StaffAppRow>[] {
  return [
    {
      key: 'name',
      header: 'Name',
      mobile: 'title',
      cell: (app) => (
        <Cluster gap={2}>
          <Link href={`/staff/apps/${app.applicationId}`} className="ui-link ui-link--plain">
            {app.slug}
          </Link>
          {app.lifecycleState !== 'ready' && <Status state={app.lifecycleState} />}
        </Cluster>
      ),
    },
    ...(showOwner
      ? [
          {
            key: 'owner',
            header: 'Owner',
            cell: (app: StaffAppRow) => <OwnerLink id={app.ownerId} name={app.ownerDisplayName} />,
          },
        ]
      : []),
    {
      key: 'repository',
      header: 'Repository',
      mobile: showOwner ? 'field' : 'secondary',
      cell: (app) => <Repository url={app.repository} />,
    },
    {
      key: 'created',
      header: 'Created',
      mobile: showOwner ? 'field' : 'meta',
      cell: (app) => <RelativeTime value={app.createdAt} />,
    },
  ];
}

function repositoryName(url: string) {
  const { hostname, pathname } = new URL(url);
  const path = pathname.replace(/^\/|\.git$|\/$/g, '');
  return hostname === 'github.com' && path ? path : hostname + (path ? `/${path}` : '');
}

export function Repository({ url }: { url: string | null }) {
  if (!url) return <span className="ui-text-subtle">Not set</span>;
  return (
    <a className="ui-link ui-break" href={url} target="_blank" rel="noopener noreferrer">
      {repositoryName(url)}
    </a>
  );
}

export function deploymentColumns(app: string, live?: string | null): Column<StaffDeployment>[] {
  return [
    {
      key: 'commit',
      header: 'Commit',
      mobile: 'title',
      cell: (deployment) => (
        <Cluster gap={2}>
          <Link
            href={`/staff/apps/${app}/deployments/${deployment.deploymentId}`}
            className="ui-link ui-link--plain"
          >
            <code>
              {deployment.repositoryCommit ? short(deployment.repositoryCommit) : 'Unknown'}
            </code>
          </Link>
          {deployment.deploymentId === live && <Status state="live" />}
        </Cluster>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      mobile: 'trailing',
      cell: (deployment) => <Status state={deployment.status} />,
    },
    {
      key: 'requested',
      header: 'Started',
      mobile: 'meta',
      cell: (deployment) => <RelativeTime value={deployment.requestedAt} />,
    },
    {
      key: 'accepted',
      header: 'Went live',
      mobile: 'hidden',
      cell: (deployment) => <RelativeTime value={deployment.acceptedAt} />,
    },
  ];
}

export function DeploymentTable({
  app,
  live,
  rows,
}: {
  app: string;
  live?: string | null;
  rows: StaffDeployment[];
}) {
  const [, navigate] = useLocation();
  return (
    <DataTable
      label="Deployments"
      columns={deploymentColumns(app, live)}
      rows={rows}
      rowKey={(deployment) => deployment.deploymentId}
      onRowClick={(deployment) =>
        navigate(`/staff/apps/${app}/deployments/${deployment.deploymentId}`)
      }
      empty={
        <EmptyState title="No deployments yet">Deploys of this app will appear here.</EmptyState>
      }
    />
  );
}

/** How long a deployment took: from start until it went live, or until it stopped. */
export function duration(deployment: StaffDeployment) {
  const end =
    deployment.acceptedAt ??
    (['failed', 'recovery_required'].includes(deployment.status) ? deployment.updatedAt : null);
  if (!deployment.requestedAt || !end) return null;
  const seconds = Math.max(
    0,
    Math.round((Date.parse(end) - Date.parse(deployment.requestedAt)) / 1000),
  );
  if (seconds < 60) return `${seconds} second${seconds === 1 ? '' : 's'}`;
  const minutes = Math.round(seconds / 60);
  return `${minutes} minute${minutes === 1 ? '' : 's'}`;
}

// Staff activity kinds that the shared titles name under another key.
const titleKinds: Record<string, string> = {
  app_enable: 'lifecycle',
  app_disable: 'lifecycle',
};
const stages: Record<string, string> = {
  queued: 'Waiting to build',
  building: 'Building',
  deploying: 'Starting',
  verifying: 'Checking health',
  recovery: 'Recovering',
};
const finished = ['succeeded', 'failed', 'blocked'];

/** One activity row. Read-only: no resume or retry actions. */
export function ActivityItem({
  item,
  showApp = true,
  showOwner = true,
}: {
  item: StaffOperation;
  showApp?: boolean;
  showOwner?: boolean;
}) {
  const progress =
    item.attention === 'awaiting_controller'
      ? 'Waiting for the platform'
      : !finished.includes(item.state)
        ? stages[item.stage]
        : undefined;
  const problem = ['failed', 'blocked'].includes(item.state);
  return (
    <ListItem
      title={activityTitle(titleKinds[item.kind] ?? item.kind, item.state)}
      meta={
        <>
          {showApp && <AppLink id={item.applicationId} name={item.applicationSlug} />}
          {showOwner && <OwnerLink id={item.ownerId} name={item.ownerDisplayName} />}
          {progress && <span>{progress}</span>}
          <RelativeTime value={item.createdAt} />
        </>
      }
      trailing={<Status state={item.state} quiet="hidden" />}
    >
      {(item.guidance || item.controllerErrorCode) && (
        <p className={`ui-text-sm ${problem ? 'ui-text-danger' : 'ui-text-muted'}`}>
          {item.guidance}
          {item.guidance && item.controllerErrorCode && ' '}
          {item.controllerErrorCode && (
            <span className="ui-text-subtle">
              Error code <code>{item.controllerErrorCode}</code>
            </span>
          )}
        </p>
      )}
    </ListItem>
  );
}

export function ActivityEmpty({ filtered }: { filtered?: boolean }) {
  return (
    <EmptyState title="No activity yet">
      {filtered
        ? 'Nothing matches this filter yet.'
        : 'New apps, saved settings and deploys will appear here.'}
    </EmptyState>
  );
}

/** "View all" for a section that previews a longer list (DESIGN.md). */
export function viewAll(href: string, more: boolean) {
  return more ? (
    <Link href={href} className={buttonClass({ variant: 'ghost', size: 'sm' })}>
      View all
      <Icon name="chevron-right" />
    </Link>
  ) : undefined;
}
