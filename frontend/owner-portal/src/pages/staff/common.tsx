import {
  Button,
  Cluster,
  DataTable,
  EmptyState,
  ErrorAlert,
  Icon,
  ListItem,
  LoadingRows,
  Section,
  backLinkClass,
  type Column,
} from '@openstack-platform/ui';
import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { Link } from 'wouter';
import { ApiError, type Page } from '../../api';
import { Status } from '../../components/Status';
import {
  staffApi,
  type StaffApp,
  type StaffCatalogApp,
  type StaffDeployment,
  type StaffOperation,
} from '../../staffApi';
import { relativeTime, short } from '../../utils/presentation';
import './staff.css';

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

export function useRead<T>(
  key: (string | undefined)[],
  read: (signal: AbortSignal) => Promise<T>,
  { poll = false, enabled = true, staleTime = 0 } = {},
) {
  const { userId, active } = useContext(StaffContext);
  return useQuery({
    queryKey: ['staff', 'staff', userId, ...key],
    queryFn: ({ signal }) => read(signal),
    enabled: active && enabled,
    staleTime,
    retry: false,
    refetchOnWindowFocus: false,
    refetchInterval: (query) =>
      !active || !poll || query.state.errorUpdateCount >= 3
        ? false
        : query.state.error instanceof ApiError
          ? Math.max(15000, query.state.error.retryAfterSeconds * 1000)
          : 15000,
    refetchIntervalInBackground: false,
  });
}

// The broker allows two active staff reads per account and one controller
// observation at a time, so pages chain related reads with `enabled`: each
// starts once the one before it has settled.

// Names for IDs. The first page of owners and apps covers a class; anything
// else falls back to a compact ID.
const lookup = { staleTime: 60000 };
export function useOwnerNames(enabled = true) {
  return useRead(['directory', 'owners'], (signal) => staffApi.owners(undefined, signal, 50), {
    ...lookup,
    enabled,
  });
}
export function useAppNames(enabled = true) {
  return useRead(
    ['directory', 'apps'],
    (signal) => staffApi.apps(undefined, undefined, signal, 50),
    { ...lookup, enabled },
  );
}
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

/** Refetches one after another to stay inside the broker's read limits. */
export function Refresh({ queries }: { queries: UseQueryResult<unknown>[] }) {
  const [now, setNow] = useState(Date.now());
  const until = Math.max(
    0,
    ...queries.map((query) =>
      query.error instanceof ApiError && [429, 503].includes(query.error.status)
        ? query.errorUpdatedAt + query.error.retryAfterSeconds * 1000
        : 0,
    ),
  );
  useEffect(() => {
    const timer = window.setTimeout(() => setNow(Date.now()), Math.max(0, until - Date.now()));
    return () => window.clearTimeout(timer);
  }, [until]);
  return (
    <Button
      onClick={async () => {
        for (const query of queries) await query.refetch();
      }}
      disabled={queries.some((query) => query.isFetching) || now < until}
    >
      Refresh
    </Button>
  );
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

/** A section body for a query: rows while loading, an alert on failure. */
export function Loaded<T>({
  query,
  children,
}: {
  query: UseQueryResult<T>;
  children: (data: T) => ReactNode;
}) {
  if (query.isPending) return <LoadingRows />;
  if (query.error)
    return (
      <div className="ui-section__body">
        <ErrorAlert error={query.error} focus={false} />
      </div>
    );
  return <>{children(query.data)}</>;
}

export function Back({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Link href={href} className={backLinkClass}>
      <Icon name="arrow-left" />
      {children}
    </Link>
  );
}

export function When({ value, empty = '—' }: { value: string | null | undefined; empty?: string }) {
  if (!value) return <span className="ui-text-subtle">{empty}</span>;
  return (
    <time dateTime={value} title={new Date(value).toLocaleString()}>
      {relativeTime(value)}
    </time>
  );
}

/** First block of an ID with a button that copies the whole ID. */
export function CopyId({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <span className="staff-id">
      <code title={value}>{value.slice(0, 8)}</code>
      <button
        type="button"
        className="staff-id__copy"
        aria-label={`Copy ${label}`}
        title={`Copy ${label}`}
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(value);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 2000);
          } catch {
            // Clipboard access denied; the full ID stays in the tooltip.
          }
        }}
      >
        <Icon name={copied ? 'check' : 'copy'} />
      </button>
      <span className="ui-sr-only" role="status">
        {copied ? 'Copied to clipboard' : ''}
      </span>
    </span>
  );
}

export function OwnerLink({ id, name }: { id: string; name?: string }) {
  return name ? (
    <Link href={`/staff/owners/${id}`} className="ui-link">
      {name}
    </Link>
  ) : (
    <CopyId value={id} label="owner ID" />
  );
}

export function AppLink({ id, name }: { id: string; name?: string }) {
  return name ? (
    <Link href={`/staff/apps/${id}`} className="ui-link">
      {name}
    </Link>
  ) : (
    <Link href={`/staff/apps/${id}`} className="ui-link">
      App <code>{id.slice(0, 8)}</code>
    </Link>
  );
}

export function Lifecycle({ state }: { state: string }) {
  return state === 'rejected' ? (
    <Status state="failed" label="Not created" />
  ) : (
    <Status state={state} />
  );
}

/** One badge for an app: being created, health, or stopped. */
export function AppHealth({ app }: { app: StaffApp }) {
  if (app.lifecycleState !== 'ready') return <Lifecycle state={app.lifecycleState} />;
  const { process, route } = app.health;
  if (app.stale) return <Status state="unknown" />;
  if (!app.desiredRunning || process === 'stopped') return <Status state="stopped" />;
  if (process === 'healthy' && route === 'healthy') return <Status state="healthy" />;
  if (process === 'unhealthy' || route === 'unhealthy') return <Status state="unhealthy" />;
  return <Status state="unknown" />;
}

const deploymentStates: Record<string, [string, string]> = {
  queued: ['prepared', 'Queued'],
  building: ['accepted', 'Building'],
  deploying: ['accepted', 'Deploying'],
  succeeded: ['succeeded', 'Succeeded'],
  failed: ['failed', 'Failed'],
  recovery_required: ['blocked', 'Needs attention'],
};
export function DeploymentStatus({ status }: { status: string }) {
  const [state, label] = deploymentStates[status] ?? ['unknown', 'Unknown'];
  return <Status state={state} label={label} />;
}

export function appColumns(owners?: Map<string, string>): Column<StaffCatalogApp>[] {
  return [
    {
      key: 'name',
      header: 'Name',
      mobile: 'title',
      cell: (app) => (
        <Link href={`/staff/apps/${app.applicationId}`} className="ui-link ui-link--plain">
          {app.slug}
        </Link>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      mobile: 'trailing',
      cell: (app) => <Lifecycle state={app.lifecycleState} />,
    },
    ...(owners
      ? [
          {
            key: 'owner',
            header: 'Owner',
            cell: (app: StaffCatalogApp) => (
              <OwnerLink id={app.ownerId} name={owners.get(app.ownerId)} />
            ),
          },
        ]
      : []),
    {
      key: 'repository',
      header: 'Repository',
      cell: (app) => <Repository url={app.repository} />,
    },
    {
      key: 'created',
      header: 'Created',
      cell: (app) => <When value={app.createdAt} />,
    },
  ];
}

export function Repository({ url }: { url: string | null }) {
  if (!url) return <span className="ui-text-subtle">Not set</span>;
  const { hostname, pathname } = new URL(url);
  const path = pathname.replace(/^\/|\.git$|\/$/g, '');
  return (
    <a className="ui-link ui-break" href={url} target="_blank" rel="noopener noreferrer">
      {hostname === 'github.com' && path ? path : hostname + (path ? `/${path}` : '')}
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
          {deployment.deploymentId === live && <Status state="running" label="Live" />}
        </Cluster>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      mobile: 'trailing',
      cell: (deployment) => <DeploymentStatus status={deployment.status} />,
    },
    {
      key: 'requested',
      header: 'Started',
      cell: (deployment) => <When value={deployment.requestedAt} />,
    },
    {
      key: 'accepted',
      header: 'Went live',
      cell: (deployment) => <When value={deployment.acceptedAt} />,
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
  return (
    <DataTable
      label="Deployments"
      columns={deploymentColumns(app, live)}
      rows={rows}
      rowKey={(deployment) => deployment.deploymentId}
      empty={
        <EmptyState title="No deployments yet">Deploys of this app will appear here.</EmptyState>
      }
    />
  );
}

const kinds: Record<string, string> = {
  create_app: 'Create app',
  save_configuration: 'Save settings',
  deploy: 'Deploy',
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
  apps,
  owners,
}: {
  item: StaffOperation;
  apps?: Map<string, string>;
  owners?: Map<string, string>;
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
      title={kinds[item.kind] ?? 'Other change'}
      meta={
        <>
          {apps && <AppLink id={item.applicationId} name={apps.get(item.applicationId)} />}
          {owners && <OwnerLink id={item.ownerId} name={owners.get(item.ownerId)} />}
          {progress && <span>{progress}</span>}
          <When value={item.createdAt} />
        </>
      }
      trailing={<Status state={item.state} />}
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

export function nameMap<T>(items: T[], id: (item: T) => string, name: (item: T) => string) {
  return new Map(items.map((item) => [id(item), name(item)]));
}

export function ActivityEmpty({ filtered }: { filtered?: boolean }) {
  return (
    <EmptyState title="No activity yet">
      {filtered
        ? 'Nothing matches these filters yet.'
        : 'New apps, saved settings and deploys will appear here.'}
    </EmptyState>
  );
}

/** Titled section with a "View all" link to the full list. */
export function Preview({
  title,
  href,
  more,
  children,
}: {
  title: string;
  href: string;
  more: boolean;
  children: ReactNode;
}) {
  return (
    <Section
      title={title}
      flush
      actions={
        more && (
          <Link href={href} className="ui-link ui-text-sm">
            View all
          </Link>
        )
      }
    >
      {children}
    </Section>
  );
}
