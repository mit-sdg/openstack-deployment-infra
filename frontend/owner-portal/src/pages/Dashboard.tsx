import { Card } from '@openstack-platform/ui';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'wouter';
import { api } from '../api';
import { BoundaryText } from '../components/BoundaryText';
import { Empty, ErrorNotice, Loading } from '../components/Feedback';
import { Operation } from '../components/Operation';
import { Status } from '../components/Status';
import { useOwnerIntents } from '../hooks/useIntentPolling';
import { healthy, short } from '../utils/presentation';

export function Dashboard() {
  const apps = useQuery({ queryKey: ['apps'], queryFn: api.apps, refetchInterval: 5000 });
  const intents = useOwnerIntents();
  if (apps.isPending) return <Loading />;
  if (apps.error) return <ErrorNotice error={apps.error} />;
  const quota = apps.data!.quota.apps;
  const full = quota.used + quota.reserved >= quota.limit;
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">Workspace</span>
          <h1>My applications</h1>
          <p>Your code, deployments, and what’s running now.</p>
        </div>
        {!full && (
          <Link className="button button-primary" href="/apps/new">
            <span aria-hidden="true">+</span> Create application
          </Link>
        )}
      </div>
      <Card className="quota-card">
        <div>
          <span className="overline">Application quota</span>
          <strong>
            {quota.used + quota.reserved}
            <span> / {quota.limit} applications</span>
          </strong>
          <p>
            {full
              ? 'Your quota is full. Contact staff if you need more space.'
              : 'Room for your next project.'}
            {quota.reserved > 0 && ` ${quota.reserved} being created.`}
          </p>
        </div>
        <progress
          max={quota.limit}
          value={quota.used + quota.reserved}
          aria-label="Applications used"
        />
        <span className="quota-note">Disabled apps still use a slot.</span>
      </Card>
      <section className="section card">
        <div className="card-header">
          <h2>
            Applications <span className="count">{apps.data!.items.length}</span>
          </h2>
          <span className="muted">Status checked automatically</span>
        </div>
        {apps.data!.items.length ? (
          <div className="table-wrap">
            <table className="app-table">
              <thead>
                <tr>
                  <th>Application</th>
                  <th>Status</th>
                  <th>Accepted commit</th>
                  <th>Settings</th>
                  <th>
                    <span className="sr-only">Open</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {apps.data!.items.map((app) => (
                  <tr key={app.applicationId}>
                    <td className="cell-app">
                      <Link href={`/apps/${app.applicationId}`} className="app-link">
                        {app.slug}
                      </Link>
                      <span className="cell-sub mono">
                        {app.url ? (
                          <BoundaryText text={new URL(app.url).hostname} />
                        ) : (
                          'Creating your application'
                        )}
                      </span>
                    </td>
                    <td className="cell-status">
                      <Status
                        state={app.lifecycleState === 'creating' ? 'creating' : healthy(app)}
                      />
                    </td>
                    <td className="cell-commit">
                      <span className="mono">{short(app.acceptedDeployment?.sourceCommit)}</span>
                    </td>
                    <td className="cell-revision">
                      <span className="chip">
                        {app.savedRevision ? `Revision ${app.savedRevision}` : 'Not configured'}
                      </span>
                    </td>
                    <td className="cell-open">
                      <Link href={`/apps/${app.applicationId}`} aria-label={`Open ${app.slug}`}>
                        →
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty title="Your first application starts here">
            Connect a public repository and deploy an exact commit.
            <br />
            <Link href="/apps/new" className="text-link">
              Create your first application →
            </Link>
          </Empty>
        )}
      </section>
      <section className="section card">
        <div className="card-header">
          <h2>Recent activity</h2>
          <span className="muted">Your operations</span>
        </div>
        <ErrorNotice error={intents.error} />
        {intents.data?.items.length ? (
          <ul className="operation-list">
            {intents.data.items.slice(0, 8).map((intent) => (
              <Operation key={intent.intentId} intent={intent} />
            ))}
          </ul>
        ) : (
          <Empty title="No operations yet">Your deployment activity will appear here.</Empty>
        )}
      </section>
    </>
  );
}
