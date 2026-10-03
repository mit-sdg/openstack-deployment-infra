import {
  Alert,
  Badge,
  BoundaryText,
  DataTable,
  EmptyState,
  ErrorAlert,
  Page,
  PageHeader,
  PageSkeleton,
  RelativeTime,
  Section,
  buttonClass,
  Icon,
  type Column,
} from '@openstack-platform/ui';
import { useQuery } from '@tanstack/react-query';
import { Link, useLocation } from 'wouter';
import { api, type AppRecord } from '../api';
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
  if (apps.isPending) return <PageSkeleton />;
  if (apps.error) return <ErrorAlert error={apps.error} />;
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
          You’ve used all {quota.apps.limit} of your apps. Ask staff if you need another one.
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
      <ErrorAlert error={intents.error} focus={false} />
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
