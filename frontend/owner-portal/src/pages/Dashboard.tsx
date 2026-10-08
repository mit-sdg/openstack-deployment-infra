import {
  Alert,
  Badge,
  BoundaryText,
  DataTable,
  EmptyState,
  Page,
  PageHeader,
  PageHeaderSkeleton,
  PageSkeleton,
  SectionSkeleton,
  RelativeTime,
  Section,
  buttonClass,
  Icon,
  type Column,
} from '@openstack-platform/ui';
import { useQuery } from '@tanstack/react-query';
import { Link, useLocation } from 'wouter';
import { api, type AppRecord } from '../api';
import { QueryError } from '../components/Feedback';
import { Operation, OperationList } from '../components/Operation';
import { Status } from '../components/Status';
import { useOwnerIntents } from '../hooks/useIntentPolling';
import { ownerAppState, short } from '../utils/presentation';

const columns: Column<AppRecord>[] = [
  {
    key: 'name',
    header: 'Name',
    mobile: 'title',
    cell: (app) => (
      <span className="ui-cluster ui-gap-2">
        <Link href={`/apps/${app.applicationId}`} className="ui-link ui-link--plain">
          {app.slug}
        </Link>
        {app.access === 'member' && (
          <span className="ui-text-muted ui-text-sm">
            {app.ownerDisplayName ? `${app.ownerDisplayName}’s app` : 'Shared'}
          </span>
        )}
      </span>
    ),
  },
  {
    key: 'status',
    header: 'Status',
    mobile: 'trailing',
    cell: (app) => <Status state={ownerAppState(app)} />,
  },
  {
    key: 'url',
    header: 'URL',
    // Phones: the whole card opens the app, so the URL stays on its page.
    mobile: 'hidden',
    cell: (app) =>
      app.url ? (
        <a className="ui-link ui-text-sm" href={app.url} target="_blank" rel="noopener noreferrer">
          <BoundaryText text={new URL(app.url).hostname} />
        </a>
      ) : (
        <span className="ui-text-subtle">—</span>
      ),
  },
  {
    key: 'commit',
    header: 'Deployed commit',
    cell: (app) =>
      app.acceptedDeployment ? (
        <code>{short(app.acceptedDeployment.sourceCommit)}</code>
      ) : (
        <span className="ui-text-subtle">Not deployed</span>
      ),
  },
  {
    key: 'deployed',
    header: 'Last deployed',
    cell: (app) => (
      <span className="ui-text-muted">
        <RelativeTime value={app.acceptedDeployment?.acceptedAt} />
      </span>
    ),
  },
];

export function Dashboard() {
  const apps = useQuery({ queryKey: ['apps'], queryFn: api.apps, refetchInterval: 5000 });
  const intents = useOwnerIntents();
  const [, navigate] = useLocation();
  if (apps.isPending)
    return (
      <PageSkeleton label="Loading your apps…">
        <PageHeaderSkeleton meta actions={1} />
        <SectionSkeleton variant="table" columns={5} rows={2} />
        <SectionSkeleton title variant="list" density="compact" rows={4} />
      </PageSkeleton>
    );
  if (apps.error)
    return (
      <Page>
        <PageHeader title="My apps" />
        <QueryError query={apps} what="your apps" />
      </Page>
    );
  const { items, quota } = apps.data;
  const used = quota.apps.used + quota.apps.reserved;
  // Accounts without limits (staff and admins) see no count and are never full.
  const limit = quota.apps.limit;
  const full = limit !== null && used >= limit;
  const create = (
    <Link href="/apps/new" className={buttonClass({ variant: 'primary' })}>
      <Icon name="plus" />
      Create app
    </Link>
  );
  return (
    <Page>
      <PageHeader
        title="My apps"
        meta={
          items.length > 0 &&
          limit !== null && (
            <Badge tone="neutral" dot={false}>
              {used} of {limit}
              <span className="ui-sr-only"> apps used</span>
            </Badge>
          )
        }
        actions={items.length > 0 && !full && create}
      />
      {full && items.length > 0 && (
        <Alert tone="info">
          You’ve reached your limit of {limit} apps. Ask staff if you need more.
        </Alert>
      )}
      {items.length ? (
        <Section flush aria-label="Your apps">
          <DataTable
            label="My apps"
            columns={columns}
            rows={items}
            rowKey={(app) => app.applicationId}
            onRowClick={(app) => navigate(`/apps/${app.applicationId}`)}
          />
        </Section>
      ) : (
        <div className="ui-card">
          <EmptyState title="Create your first app" action={!full && create}>
            Connect a GitHub repository, then deploy any commit.
          </EmptyState>
        </div>
      )}
      {intents.error && <QueryError query={intents} what="recent activity" />}
      {!!intents.data?.items.length && (
        <Section title="Recent activity" flush>
          <OperationList label="Recent activity">
            {intents.data.items.slice(0, 8).map((intent) => (
              <Operation key={intent.intentId} intent={intent} />
            ))}
          </OperationList>
        </Section>
      )}
    </Page>
  );
}
