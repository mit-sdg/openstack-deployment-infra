import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'wouter';
import { api } from '../api';
import { AppFrame } from '../components/AppFrame';
import { DeploymentRow } from '../components/DeploymentRow';
import { Empty, ErrorNotice, Loading } from '../components/Feedback';

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
      <div className="section-heading">
        <div>
          <h2>Deployment history</h2>
          <p>Every attempt retains its exact source and configuration.</p>
        </div>
        <Link href={`/apps/${id}/deploy`} className="button button-primary">
          Deploy a commit
        </Link>
      </div>
      <section className="card">
        {history.isPending ? (
          <Loading />
        ) : history.error ? (
          <ErrorNotice error={history.error} />
        ) : history.data!.items.length ? (
          <>
            {history.data!.items.map((deployment) => (
              <DeploymentRow
                key={deployment.deploymentId}
                id={id}
                deployment={deployment}
                active={app.data?.activeDeploymentId}
              />
            ))}
            <div className="pagination">
              {cursor && (
                <button className="button" onClick={() => setCursor(undefined)}>
                  Newest deployments
                </button>
              )}
              {history.data!.nextCursor && (
                <button className="button" onClick={() => setCursor(history.data!.nextCursor!)}>
                  Older deployments →
                </button>
              )}
            </div>
          </>
        ) : (
          <Empty title="No deployments yet">
            <Link href={`/apps/${id}/deploy`}>Choose your first commit →</Link>
          </Empty>
        )}
      </section>
    </AppFrame>
  );
}
