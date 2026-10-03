import {
  Alert,
  EmptyState,
  Icon,
  KeyValueList,
  List,
  PageSkeleton,
  RelativeTime,
  Section,
  SectionSkeleton,
  buttonClass,
} from '@openstack-platform/ui';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'wouter';
import { api, type AppRecord } from '../api';
import { AppFrame } from '../components/AppFrame';
import { DeploymentRow } from '../components/DeploymentRow';
import { QueryError } from '../components/Feedback';
import { Operation, OperationList } from '../components/Operation';
import { Status } from '../components/Status';
import { useOwnerIntents } from '../hooks/useIntentPolling';
import { ownerAppState, short } from '../utils/presentation';

function Running({ app }: { app: AppRecord }) {
  const deployed = app.acceptedDeployment!;
  const items = [
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
    { label: 'Deployed', value: <RelativeTime value={deployed.acceptedAt} /> },
  ];
  // The header already shows the app's state. Break it down only when it
  // isn't healthy, so the person can see which part needs attention.
  const state = ownerAppState(app);
  if (!app.stale && (state === 'unhealthy' || state === 'unknown')) {
    const health = app.health;
    items.push(
      {
        label: 'App',
        value: <Status state={health?.allocationHealthy === true ? 'healthy' : 'unknown'} />,
      },
      {
        label: 'Public URL',
        value: (
          <Status
            state={health?.routeHealthy === true ? 'healthy' : 'unknown'}
            label={health?.routeHealthy === true ? undefined : 'Not checked yet'}
          />
        ),
      },
    );
  }
  return <KeyValueList columns={2} items={items} />;
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
        <PageSkeleton label="Loading your app…">
          <SectionSkeleton title rows={1} />
          <SectionSkeleton title variant="list" rows={1} />
          <SectionSkeleton title variant="list" density="compact" rows={4} />
        </PageSkeleton>
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
            {history.error && <QueryError query={history} what="deployments" />}
            {latest && (
              <Section
                title="Latest deployment"
                flush
                actions={
                  (history.data!.items.length > 1 || history.data!.nextCursor) && (
                    <Link
                      href={`/apps/${id}/deployments`}
                      className={buttonClass({ variant: 'ghost', size: 'sm' })}
                    >
                      View all
                      <Icon name="chevron-right" />
                    </Link>
                  )
                }
              >
                <List label="Latest deployment">
                  <DeploymentRow id={id} deployment={latest} active={app.data.activeDeploymentId} />
                </List>
              </Section>
            )}
            {intents.error && <QueryError query={intents} what="activity" />}
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
