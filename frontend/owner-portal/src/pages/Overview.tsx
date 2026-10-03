import { useQuery } from '@tanstack/react-query';
import { Link } from 'wouter';
import { api } from '../api';
import { AppFrame } from '../components/AppFrame';
import { DeploymentRow } from '../components/DeploymentRow';
import { Loading } from '../components/Feedback';
import { Operation } from '../components/Operation';
import { Status } from '../components/Status';
import { useOwnerIntents } from '../hooks/useIntentPolling';
import { healthy, short, time } from '../utils/presentation';

export function Overview({ id }: { id: string }) {
  const app = useQuery({
    queryKey: ['app', id],
    queryFn: () => api.app(id),
    refetchInterval: 5000,
  });
  const history = useQuery({
    queryKey: ['history', id],
    queryFn: () => api.history(id),
    refetchInterval: 5000,
  });
  const intents = useOwnerIntents();
  return (
    <AppFrame id={id} active="Overview">
      {app.isPending ? (
        <Loading />
      ) : (
        app.data && (
          <>
            <div className="overview-grid">
              <section className="card overview-card">
                <span className="overline">Accepted deployment</span>
                <h2>
                  {app.data.activeDeploymentId
                    ? 'Your application is deployed'
                    : 'Ready for your first deployment'}
                </h2>
                <p>
                  {app.data.activeDeploymentId
                    ? 'This is the version the platform has accepted. Newer attempts appear separately in your history.'
                    : 'Save your repository and runtime settings, then choose the exact commit to deploy.'}
                </p>
                {app.data.configurationChanged && (
                  <p className="notice">
                    Configuration changed since last deploy. Deploy again to apply saved bindings or
                    rotated credentials.
                  </p>
                )}
                <div className="overview-meta">
                  <div>
                    <span>Commit</span>
                    <strong className="mono">
                      {short(app.data.acceptedDeployment?.sourceCommit)}
                    </strong>
                  </div>
                  <div>
                    <span>Saved settings</span>
                    <strong>
                      {app.data.savedRevision
                        ? `Revision ${app.data.savedRevision}`
                        : 'Not configured'}
                    </strong>
                  </div>
                  <div>
                    <span>Accepted</span>
                    <strong>{time(app.data.acceptedDeployment?.acceptedAt)}</strong>
                  </div>
                </div>
                <Link
                  className="button button-primary"
                  href={`/apps/${id}/${app.data.savedRevision ? 'deploy' : 'configuration'}`}
                >
                  {app.data.savedRevision ? 'Deploy a commit' : 'Configure application'} →
                </Link>
              </section>
              <section className="card health-card">
                <span className="overline">Application health</span>
                <Status state={healthy(app.data)} />
                <dl>
                  <div>
                    <dt>Application process</dt>
                    <dd>
                      {app.data.health?.allocationHealthy === true
                        ? 'Healthy'
                        : !app.data.desiredRunning
                          ? 'Stopped'
                          : 'Unknown'}
                    </dd>
                  </div>
                  <div>
                    <dt>Public route</dt>
                    <dd>{app.data.health?.routeHealthy === true ? 'Healthy' : 'Not observed'}</dd>
                  </div>
                </dl>
                <p className="field-help">
                  {app.data.stale
                    ? 'The latest observation is unavailable.'
                    : `Checked ${time(app.data.observedAt)}`}
                </p>
              </section>
            </div>
            {history.data?.items[0] && (
              <section className="section card">
                <div className="card-header">
                  <h2>Latest attempt</h2>
                  <Link href={`/apps/${id}/deployments`}>All deployments →</Link>
                </div>
                <DeploymentRow
                  id={id}
                  deployment={history.data.items[0]}
                  active={app.data.activeDeploymentId}
                />
              </section>
            )}
            <section className="section card">
              <div className="card-header">
                <h2>Application activity</h2>
              </div>
              <ul className="operation-list">
                {intents.data?.items
                  .filter((intent) => intent.appId === id)
                  .slice(0, 6)
                  .map((intent) => (
                    <Operation key={intent.intentId} intent={intent} />
                  ))}
              </ul>
            </section>
          </>
        )
      )}
    </AppFrame>
  );
}
