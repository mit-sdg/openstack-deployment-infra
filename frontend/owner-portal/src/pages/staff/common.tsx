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
  buttonClass,
  type Column,
} from '@openstack-platform/ui';
import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { Link, useLocation } from 'wouter';
import { ApiError, type Page } from '../../api';
import { Status } from '../../components/Status';
import {
  staffApi,
  type StaffApp,
  type StaffAppRow,
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
      variant="ghost"
      size="sm"
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
export function CopyId({
  value,
  label,
  length = 8,
}: {
  value: string;
  label: string;
  /** Characters shown; commits use 9 to match the rest of the portal. */
  length?: number;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <span className="staff-id">
      <code title={value}>{value.slice(0, length)}</code>
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

/**
 * Secondary text under a table row's title, shown only on phones, where the
 * columns it summarises are hidden (candidate for DataTable in shared).
 */
export function PhoneDetail({ children }: { children: ReactNode }) {
  return <span className="staff-row-detail">{children}</span>;
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

/** One health check as a detail value: plain text, a badge only when it needs a look. */
export function HealthValue({ state }: { state: string }) {
  if (state === 'healthy') return <>Healthy</>;
  if (state === 'stopped') return <>Stopped</>;
  return <Status state={state} label={state === 'unknown' ? 'Unknown' : undefined} />;
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

/** App table columns; without the owner column, phones show two-line rows. */
export function appColumns(showOwner: boolean): Column<StaffAppRow>[] {
  const detail = showOwner ? 'field' : 'hidden';
  return [
    {
      key: 'name',
      header: 'Name',
      mobile: 'title',
      cell: (app) => (
        <>
          <Link href={`/staff/apps/${app.applicationId}`} className="ui-link ui-link--plain">
            {app.slug}
          </Link>
          {!showOwner && (
            <PhoneDetail>
              {repositoryName(app.repository) ?? 'No repository'}
              {app.createdAt && ` · ${relativeTime(app.createdAt)}`}
            </PhoneDetail>
          )}
        </>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      mobile: 'trailing',
      cell: (app) => <Lifecycle state={app.lifecycleState} />,
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
      mobile: detail,
      cell: (app) => <Repository url={app.repository} />,
    },
    {
      key: 'created',
      header: 'Created',
      mobile: detail,
      cell: (app) => <When value={app.createdAt} />,
    },
  ];
}

function repositoryName(url: string | null) {
  if (!url) return null;
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
          {deployment.deploymentId === live && <Status state="running" label="Live" />}
          {deployment.requestedAt && (
            <PhoneDetail>Started {relativeTime(deployment.requestedAt)}</PhoneDetail>
          )}
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
      mobile: 'hidden',
      cell: (deployment) => <When value={deployment.requestedAt} />,
    },
    {
      key: 'accepted',
      header: 'Went live',
      mobile: 'hidden',
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
      title={kinds[item.kind] ?? 'Other change'}
      meta={
        <>
          {showApp && <AppLink id={item.applicationId} name={item.applicationSlug} />}
          {showOwner && <OwnerLink id={item.ownerId} name={item.ownerDisplayName} />}
          {progress && <span>{progress}</span>}
          <When value={item.createdAt} />
        </>
      }
      trailing={
        // Success is the norm in feeds: no badge, but screen readers hear it.
        item.state === 'succeeded' ? (
          <span className="ui-sr-only">Succeeded</span>
        ) : (
          <Status state={item.state} />
        )
      }
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
