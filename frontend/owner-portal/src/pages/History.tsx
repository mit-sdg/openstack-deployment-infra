import {
  Button,
  Cluster,
  DataTable,
  EmptyState,
  PageSkeleton,
  RelativeTime,
  Section,
  SectionSkeleton,
  buttonClass,
  type Column,
} from '@openstack-platform/ui';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Link, useLocation } from 'wouter';
import { api, type Deployment } from '../api';
import { AppFrame } from '../components/AppFrame';
import { QueryError } from '../components/Feedback';
import { Status } from '../components/Status';
import { short } from '../utils/presentation';

function columns(id: string, active: string | null | undefined): Column<Deployment>[] {
  return [
    {
      key: 'commit',
      header: 'Commit',
      mobile: 'title',
      cell: (deployment) => (
        <Link
          href={`/apps/${id}/deployments/${deployment.deploymentId}`}
          className="ui-link ui-link--plain ui-mono"
        >
          {short(deployment.repositoryCommit)}
        </Link>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      mobile: 'trailing',
      // The live deployment reads "Live"; it succeeded by definition.
      cell: (deployment) => (
        <Status state={deployment.deploymentId === active ? 'live' : deployment.status} />
      ),
    },
    {
      key: 'started',
      header: 'Started',
      mobile: 'meta',
      cell: (deployment) => (
        <span className="ui-text-muted">
          <RelativeTime value={deployment.requestedAt} />
        </span>
      ),
    },
    {
      key: 'deploy',
      header: 'Deploy again',
      mobile: 'field',
      cell: (deployment) => (
        <Link
          href={`/apps/${id}/deploy?commit=${deployment.repositoryCommit}`}
          className={buttonClass({ size: 'sm', variant: 'ghost' })}
        >
          Deploy this commit again
        </Link>
      ),
    },
    {
      key: 'live',
      header: 'Went live',
      mobile: 'hidden',
      cell: (deployment) => (
        <span className="ui-text-muted">
          <RelativeTime value={deployment.acceptedAt} />
        </span>
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
  const [, navigate] = useLocation();
  return (
    <AppFrame id={id} active="Deployments">
      {history.isPending ? (
        <PageSkeleton label="Loading deployments…">
          <SectionSkeleton variant="table" columns={4} rows={3} />
        </PageSkeleton>
      ) : history.error ? (
        <QueryError query={history} what="deployments" />
      ) : history.data.items.length ? (
        <>
          <Section flush aria-label="Deployments">
            <DataTable
              label="Deployments"
              columns={columns(id, app.data?.activeDeploymentId)}
              rows={history.data.items}
              rowKey={(deployment) => deployment.deploymentId}
              onRowClick={(deployment) =>
                navigate(`/apps/${id}/deployments/${deployment.deploymentId}`)
              }
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
