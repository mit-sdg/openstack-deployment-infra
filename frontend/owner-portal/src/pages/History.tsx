import {
  Badge,
  Button,
  Cluster,
  DataTable,
  EmptyState,
  ErrorAlert,
  LoadingRows,
  Section,
  buttonClass,
  type Column,
} from '@openstack-platform/ui';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'wouter';
import { api, type Deployment } from '../api';
import { AppFrame } from '../components/AppFrame';
import { Status } from '../components/Status';
import { relativeTime, short } from '../utils/presentation';

function columns(id: string, active: string | null | undefined): Column<Deployment>[] {
  return [
    {
      key: 'commit',
      header: 'Commit',
      mobile: 'title',
      cell: (deployment) => (
        <span className="app-inline">
          <Link
            href={`/apps/${id}/deployments/${deployment.deploymentId}`}
            className="ui-link ui-link--plain ui-mono"
          >
            {short(deployment.repositoryCommit)}
          </Link>
          {deployment.deploymentId === active && (
            <Badge tone="neutral" dot={false}>
              Live
            </Badge>
          )}
        </span>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      mobile: 'trailing',
      cell: (deployment) => <Status state={deployment.status} />,
    },
    {
      key: 'started',
      header: 'Started',
      cell: (deployment) => (
        <time
          className="ui-text-muted"
          dateTime={deployment.requestedAt}
          title={new Date(deployment.requestedAt).toLocaleString()}
        >
          {relativeTime(deployment.requestedAt)}
        </time>
      ),
    },
    {
      key: 'live',
      header: 'Went live',
      cell: (deployment) =>
        deployment.acceptedAt ? (
          <time
            className="ui-text-muted"
            dateTime={deployment.acceptedAt}
            title={new Date(deployment.acceptedAt).toLocaleString()}
          >
            {relativeTime(deployment.acceptedAt)}
          </time>
        ) : (
          <span className="ui-text-subtle">—</span>
        ),
    },
  ];
}

export function HistoryPage({ id }: { id: string }) {
  const [cursor, setCursor] = useState<string | undefined>();
  const history = useQuery({
    queryKey: ['history', id, cursor],
    queryFn: () => api.history(id, cursor),
    refetchInterval: 5000,
  });
  const app = useQuery({ queryKey: ['app', id], queryFn: () => api.app(id) });
  return (
    <AppFrame id={id} active="Deployments">
      {history.isPending ? (
        <Section flush aria-label="Deployments">
          <LoadingRows />
        </Section>
      ) : history.error ? (
        <ErrorAlert error={history.error} />
      ) : history.data.items.length ? (
        <>
          <Section flush aria-label="Deployments">
            <DataTable
              label="Deployments"
              columns={columns(id, app.data?.activeDeploymentId)}
              rows={history.data.items}
              rowKey={(deployment) => deployment.deploymentId}
            />
          </Section>
          {(cursor || history.data.nextCursor) && (
            <Cluster justify="end">
              {cursor && <Button onClick={() => setCursor(undefined)}>Newest</Button>}
              {history.data.nextCursor && (
                <Button onClick={() => setCursor(history.data.nextCursor!)}>Older</Button>
              )}
            </Cluster>
          )}
        </>
      ) : (
        <div className="ui-card">
          <EmptyState
            title="No deployments yet"
            action={
              <Link href={`/apps/${id}/deploy`} className={buttonClass({ variant: 'primary' })}>
                Deploy
              </Link>
            }
          >
            Each deployment of your app will appear here with its commit and build output.
          </EmptyState>
        </div>
      )}
    </AppFrame>
  );
}
