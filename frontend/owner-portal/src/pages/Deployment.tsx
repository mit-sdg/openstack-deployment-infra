import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'wouter';
import { api } from '../api';
import { AppFrame } from '../components/AppFrame';
import { BoundaryText } from '../components/BoundaryText';
import { ErrorNotice, Loading } from '../components/Feedback';
import { Status } from '../components/Status';
import { short, time } from '../utils/presentation';

export function DeploymentPage({ id, deployment }: { id: string; deployment: string }) {
  const [paused, setPaused] = useState(false);
  const attempt = useQuery({
    queryKey: ['deployment', id, deployment],
    queryFn: () => api.deployment(id, deployment),
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 1500 : false),
  });
  const log = useQuery({
    queryKey: ['log', id, deployment],
    queryFn: () => api.log(id, deployment),
    enabled: !!attempt.data,
    refetchInterval: !paused && attempt.data?.status === 'running' ? 1500 : false,
  });
  return (
    <AppFrame id={id} active="Deployments">
      <Link href={`/apps/${id}/deployments`} className="back-link">
        ← Deployment history
      </Link>
      {attempt.isPending ? (
        <Loading />
      ) : attempt.error ? (
        <ErrorNotice error={attempt.error} />
      ) : (
        <>
          <div className="section-heading">
            <div>
              <span className="eyebrow">Deployment attempt</span>
              <h2 className="mono">{short(attempt.data!.repositoryCommit)}</h2>
              <p>{time(attempt.data!.requestedAt)}</p>
            </div>
            <Status state={attempt.data!.status} />
          </div>
          <section className="card deployment-detail">
            <dl className="kv">
              <dt>Exact commit</dt>
              <dd className="mono break-text">{attempt.data!.repositoryCommit}</dd>
              <dt>Source repository</dt>
              <dd className="break-text">
                <BoundaryText text={attempt.data!.sourceRepository} />
              </dd>
              <dt>Settings revision</dt>
              <dd>{attempt.data!.configurationRevision}</dd>
              <dt>Runtime</dt>
              <dd>{attempt.data!.configuration.build.runtime === 'node' ? 'Node.js' : 'Bun'}</dd>
              <dt>Artifact</dt>
              <dd className="mono break-text">
                {attempt.data!.imageDigest ?? 'Not published yet'}
              </dd>
              <dt>Cleanup</dt>
              <dd>{attempt.data!.cleanupState}</dd>
            </dl>
          </section>
          <section className="section card log-card">
            <div className="card-header">
              <h2>Build output</h2>
              <div className="log-actions">
                <button className="button button-small" onClick={() => setPaused(!paused)}>
                  {paused ? 'Resume updates' : 'Pause updates'}
                </button>
                <button className="button button-small" onClick={() => log.refetch()}>
                  Refresh
                </button>
              </div>
            </div>
            <ErrorNotice error={log.error} />
            <pre tabIndex={0} aria-label="Build log" className="log-viewer">
              {log.data?.text.replace(/\x1b\[[0-9;]*[A-Za-z]/g, '') ||
                'Build output will appear here.'}
            </pre>
            <div className="log-footer">
              <span>
                {log.data?.truncated
                  ? 'Output is bounded; older lines may be omitted.'
                  : 'Showing bounded build output.'}
              </span>
              <span>Application output may contain sensitive text.</span>
            </div>
          </section>
        </>
      )}
    </AppFrame>
  );
}
