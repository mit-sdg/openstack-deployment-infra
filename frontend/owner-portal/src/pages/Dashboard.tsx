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
import { healthy, short } from '../utils/presentation';

const columns: Column<AppRecord>[] = [
  {
    key: 'name',
    header: 'Name',
    mobile: 'title',
    cell: (app) => (
      <Link href={`/apps/${app.applicationId}`} className="ui-link ui-link--plain">
        {app.slug}
      </Link>
    ),
  },
  {
    key: 'status',
    header: 'Status',
    mobile: 'trailing',
    cell: (app) => <Status state={app.lifecycleState === 'creating' ? 'creating' : healthy(app)} />,
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
        <PageHeader title="Apps" />
        <QueryError query={apps} what="your apps" />
      </Page>
    );
  const { items, quota } = apps.data;
  const used = quota.apps.used + quota.apps.reserved;
  const full = used >= quota.apps.limit;
  const create = (
    <Link href="/apps/new" className={buttonClass({ variant: 'primary' })}>
      <Icon name="plus" />
      Create app
    </Link>
  );
  return (
    <Page>
      <PageHeader
        title="Apps"
        meta={
          items.length > 0 && (
            <Badge tone="neutral" dot={false}>
              {used} of {quota.apps.limit}
              <span className="ui-sr-only"> apps used</span>
            </Badge>
          )
        }
        actions={items.length > 0 && !full && create}
      />
      {full && items.length > 0 && (
        <Alert tone="info">
          You’ve reached your limit of {quota.apps.limit} apps. Ask staff if you need more.
        </Alert>
      )}
      {items.length ? (
        <Section flush aria-label="Your apps">
          <DataTable
            label="Apps"
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
