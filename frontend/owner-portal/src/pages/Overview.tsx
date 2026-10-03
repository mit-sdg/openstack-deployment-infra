import {
  Alert,
  EmptyState,
  ErrorAlert,
  KeyValueList,
  List,
  LoadingRows,
  Section,
  buttonClass,
} from '@openstack-platform/ui';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'wouter';
import { api, type AppRecord } from '../api';
import { AppFrame } from '../components/AppFrame';
import { DeploymentRow } from '../components/DeploymentRow';
import { Operation, OperationList } from '../components/Operation';
import { Status } from '../components/Status';
import { useOwnerIntents } from '../hooks/useIntentPolling';
import { relativeTime, short } from '../utils/presentation';

function When({ value }: { value: string | null | undefined }) {
  return value ? (
    <time dateTime={value} title={new Date(value).toLocaleString()}>
      {relativeTime(value)}
    </time>
  ) : (
    <span className="ui-text-subtle">—</span>
  );
}

function Running({ app }: { app: AppRecord }) {
  const health = app.health;
  const process =
    health?.allocationHealthy === true ? 'healthy' : !app.desiredRunning ? 'stopped' : 'unknown';
  const route = health?.routeHealthy === true ? 'healthy' : 'unknown';
  const deployed = app.acceptedDeployment!;
  return (
    <KeyValueList
      columns={2}
      items={[
        {
          label: 'Commit',
          value: (
            <Link
              href={`/apps/${app.applicationId}/deployments/${deployed.deploymentId}`}
              className="ui-link ui-mono"
            >
              {short(deployed.sourceCommit)}
            </Link>
          ),
        },
        { label: 'Deployed', value: <When value={deployed.acceptedAt} /> },
        { label: 'App', value: <Status state={app.stale ? 'unknown' : process} /> },
        {
          label: 'Public URL',
          value: (
            <Status
              state={app.stale ? 'unknown' : route}
              label={!app.stale && route === 'unknown' ? 'Not checked yet' : undefined}
            />
          ),
        },
      ]}
    />
  );
}

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
  const activity = intents.data?.items.filter((intent) => intent.appId === id).slice(0, 6) ?? [];
  const latest = history.data?.items[0];
  const deploy = (
    <Link href={`/apps/${id}/deploy`} className={buttonClass({ variant: 'primary' })}>
      Deploy
    </Link>
  );
  return (
    <AppFrame id={id} active="Overview">
      {app.isPending ? (
        <Section title="Current deployment">
          <LoadingRows />
        </Section>
      ) : (
        app.data && (
          <>
            {app.data.stale && !!app.data.savedRevision && (
              <Alert tone="warning">
                Health information is unavailable right now. It updates again automatically.
              </Alert>
            )}
            {app.data.configurationChanged && (
              <Alert
                tone="info"
                action={
                  <Link href={`/apps/${id}/deploy`} className={buttonClass({ size: 'sm' })}>
                    Deploy
                  </Link>
                }
              >
                Settings changed since the last deploy. Deploy again to apply them.
              </Alert>
            )}
            {!app.data.savedRevision ? (
              <div className="ui-card">
                <EmptyState
                  title="Set up your app"
                  action={
                    <Link
                      href={`/apps/${id}/configuration`}
                      className={buttonClass({ variant: 'primary' })}
                    >
                      Open settings
                    </Link>
                  }
                >
                  Add your GitHub repository and how to start your app, then deploy any commit.
                </EmptyState>
              </div>
            ) : app.data.acceptedDeployment ? (
              <Section title="Current deployment" actions={deploy}>
                <Running app={app.data} />
              </Section>
            ) : (
              <div className="ui-card">
                <EmptyState title="Not deployed yet" action={deploy}>
                  Deploy a commit to put your app online.
                </EmptyState>
              </div>
            )}
            <ErrorAlert error={history.error} focus={false} />
            {latest && (
              <Section
                title="Latest deployment"
                flush
                actions={
                  <Link
                    href={`/apps/${id}/deployments`}
                    className={buttonClass({ variant: 'ghost', size: 'sm' })}
                  >
                    View all
                  </Link>
                }
              >
                <List label="Latest deployment">
                  <DeploymentRow id={id} deployment={latest} active={app.data.activeDeploymentId} />
                </List>
              </Section>
            )}
            <ErrorAlert error={intents.error} focus={false} />
            {!!activity.length && (
              <Section title="Activity" flush>
                <OperationList label="Activity">
                  {activity.map((intent) => (
                    <Operation key={intent.intentId} intent={intent} showApp={false} />
                  ))}
                </OperationList>
              </Section>
            )}
          </>
        )
      )}
    </AppFrame>
  );
}
