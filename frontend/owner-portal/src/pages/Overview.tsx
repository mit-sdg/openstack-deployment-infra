import {
  Alert,
  Button,
  Checkbox,
  Cluster,
  Dialog,
  ErrorAlert,
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
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'wouter';
import { api, type AppRecord } from '../api';
import { AppFrame } from '../components/AppFrame';
import { DeploymentRow } from '../components/DeploymentRow';
import { QueryError } from '../components/Feedback';
import { Operation, OperationList } from '../components/Operation';
import { useIntentPolling } from '../hooks/useIntentPolling';
import { Status } from '../components/Status';
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

function RuntimeActions({ app }: { app: AppRecord }) {
  const [action, setAction] = useState<'stop' | 'start' | 'restart' | null>(null);
  const [consent, setConsent] = useState(false);
  const [key, setKey] = useState('');
  const [intentId, setIntentId] = useState<string | null>(null);
  const intent = useIntentPolling(intentId);
  const client = useQueryClient();
  const change = useMutation({
    mutationFn: () =>
      action === 'restart'
        ? api.restart(app.applicationId, key, consent)
        : api.state(app.applicationId, action === 'start', key, consent),
    onSuccess: (result) => {
      setIntentId(result.intentId);
      setAction(null);
      client.invalidateQueries({ queryKey: ['activity', app.applicationId] });
    },
  });
  const busy =
    change.isPending || (!!intent.data && !['succeeded', 'failed'].includes(intent.data.state));
  function open(next: 'stop' | 'start' | 'restart') {
    change.reset();
    setConsent(false);
    setKey(crypto.randomUUID());
    setAction(next);
  }
  return (
    <>
      <Section title="App controls">
        <Cluster>
          <Button disabled={busy} onClick={() => open(app.desiredRunning ? 'stop' : 'start')}>
            {app.desiredRunning ? 'Stop app' : 'Start app'}
          </Button>
          <Button disabled={busy || !app.desiredRunning} onClick={() => open('restart')}>
            Restart app
          </Button>
        </Cluster>
        {intent.data && (
          <OperationList label="App control activity">
            <Operation intent={intent.data} showApp={false} />
          </OperationList>
        )}
      </Section>
      <Dialog
        open={!!action}
        onClose={() => setAction(null)}
        title={`${action === 'stop' ? 'Stop' : action === 'start' ? 'Start' : 'Restart'} ${app.slug}?`}
        footer={
          <>
            <Button onClick={() => setAction(null)}>Cancel</Button>
            <Button
              variant={action === 'stop' ? 'danger' : 'primary'}
              loading={change.isPending}
              disabled={app.identityProvider && !consent}
              onClick={() => change.mutate()}
            >
              {action === 'stop' ? 'Stop app' : action === 'start' ? 'Start app' : 'Restart app'}
            </Button>
          </>
        }
      >
        <ErrorAlert error={change.error} />
        <p>
          {action === 'stop'
            ? 'The app goes offline and frees its server. Its settings and data are kept.'
            : action === 'start'
              ? 'The app comes back with its last deployed version.'
              : 'The app restarts with its current version on the same server. There will be a brief interruption.'}
        </p>
        {app.identityProvider && (
          <Checkbox
            checked={consent}
            onChange={(event) => setConsent(event.target.checked)}
            label="I understand this interrupts portal sign-in"
          />
        )}
      </Dialog>
    </>
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
  // Everyone's changes to this app, so teammates see each other's deploys.
  const intents = useQuery({
    queryKey: ['activity', id],
    queryFn: () => api.activity(id),
    refetchInterval: 5000,
  });
  const activity = intents.data?.slice(0, 6) ?? [];
  const latest = history.data?.items[0];
  const deploy = (
    <>
      <Link href={`/apps/${id}/deploy?latest=1`} className={buttonClass()}>
        Deploy latest
      </Link>
      <Link href={`/apps/${id}/deploy`} className={buttonClass({ variant: 'primary' })}>
        Deploy
      </Link>
    </>
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
            {app.data.acceptedDeployment && <RuntimeActions app={app.data} />}
            {history.error && <QueryError query={history} what="deployments" />}
            {/* The current deployment is already shown above; list the latest
                only when a newer attempt is in progress or failed. */}
            {latest && latest.deploymentId !== app.data.acceptedDeployment?.deploymentId && (
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
