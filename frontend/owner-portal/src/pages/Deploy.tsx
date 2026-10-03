import {
  Alert,
  Button,
  Checkbox,
  Dialog,
  EmptyState,
  ErrorAlert,
  Field,
  Grid,
  Hint,
  Input,
  KeyValueList,
  LoadingRows,
  Section,
  buttonClass,
} from '@openstack-platform/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { Link } from 'wouter';
import { api, type Settings } from '../api';
import { AppFrame } from '../components/AppFrame';
import { BoundaryText } from '../components/BoundaryText';
import { Operation, OperationList } from '../components/Operation';
import { useIntentPolling } from '../hooks/useIntentPolling';

function Names({ names }: { names: string[] }) {
  return names.length ? (
    <span className="app-names">
      {names.map((name) => (
        <code key={name}>{name}</code>
      ))}
    </span>
  ) : (
    <span className="ui-text-subtle">None</span>
  );
}

function summary(settings: Settings, names: string[]) {
  return [
    { label: 'Repository', value: <BoundaryText text={settings.repository} /> },
    { label: 'Branch', value: settings.branch },
    {
      label: 'Runtime',
      value: settings.configuration.build.runtime === 'node' ? 'Node.js' : 'Bun',
    },
    {
      label: 'Start script',
      value: <code>{settings.configuration.build.startScript}</code>,
    },
    { label: 'Health check', value: <code>{settings.configuration.runtime.healthPath}</code> },
    { label: 'Environment variables', value: <Names names={names} /> },
  ];
}

export function DeployPage({ id }: { id: string }) {
  const settings = useQuery({ queryKey: ['settings', id], queryFn: () => api.settings(id) });
  const environment = useQuery({
    queryKey: ['environment', id],
    queryFn: () => api.environment(id),
  });
  const injectedNames = [
    ...new Set([
      ...(environment.data?.items.map((item) => item.name) ?? []),
      ...(settings.data?.configuration.storageBindings.flatMap((binding) =>
        Object.values(binding.outputs),
      ) ?? []),
    ]),
  ].sort();
  const identity = useQuery({ queryKey: ['app', id], queryFn: () => api.app(id) });
  const [identityConfirmed, setIdentityConfirmed] = useState(false);
  const [sha, setSha] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [intentId, setIntentId] = useState<string | null>(null);
  const [review, setReview] = useState(false);
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const client = useQueryClient();
  const intent = useIntentPolling(intentId);
  const deploy = useMutation({
    mutationFn: (key: string) =>
      api.deploy(id, settings.data!.revision, sha, key, identityConfirmed),
    onSuccess: (result) => {
      setIntentId(result.intentId);
      setReview(false);
      client.invalidateQueries({ queryKey: ['intents'] });
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    if (!/^[a-f0-9]{40}$/.test(sha)) {
      setError('Enter the full 40-character commit SHA in lowercase.');
      return;
    }
    setError(null);
    setReview(true);
  }
  const needsIdentity = identity.data?.identityProvider === true;
  return (
    <AppFrame id={id} active="Deploy">
      {settings.isPending ? (
        <Section title="Deploy a commit">
          <LoadingRows />
        </Section>
      ) : settings.error ? (
        <ErrorAlert error={settings.error} />
      ) : settings.data.revision === 0 ? (
        <div className="ui-card">
          <EmptyState
            title="Set up your app first"
            action={
              <Link
                href={`/apps/${id}/configuration`}
                className={buttonClass({ variant: 'primary' })}
              >
                Open settings
              </Link>
            }
          >
            Add your GitHub repository and how to start your app, then come back to deploy.
          </EmptyState>
        </div>
      ) : (
        <>
          {intent.data && (
            <Section title="Deployment progress" flush>
              <OperationList label="Deployment progress">
                <Operation intent={intent.data} showApp={false} />
              </OperationList>
            </Section>
          )}
          {intent.data?.state === 'succeeded' && (
            <Alert
              tone="success"
              action={
                <Link href={`/apps/${id}`} className={buttonClass({ size: 'sm' })}>
                  View app
                </Link>
              }
            >
              Deployment succeeded. Your app is running this commit.
            </Alert>
          )}
          <Grid columns={2} gap={6} className="app-deploy">
            <form onSubmit={submit} noValidate>
              <Section
                title="Deploy a commit"
                footer={
                  <Button type="submit" variant="primary" disabled={deploy.isPending}>
                    Review deployment
                  </Button>
                }
              >
                <Field
                  label="Commit SHA"
                  id="commit"
                  error={error}
                  hint="Copy the full SHA from GitHub. The deployment always uses this exact commit, even if the branch moves on."
                >
                  <Input
                    className="ui-mono"
                    value={sha}
                    onChange={(event) => {
                      setSha(event.target.value.trim());
                      setPendingKey(null);
                    }}
                    placeholder="40 characters, 0–9 and a–f"
                    autoComplete="off"
                    autoCapitalize="none"
                    spellCheck={false}
                    maxLength={40}
                  />
                </Field>
              </Section>
            </form>
            <Section
              title="Settings"
              actions={
                <Link
                  href={`/apps/${id}/configuration`}
                  className={buttonClass({ variant: 'ghost', size: 'sm' })}
                >
                  Edit
                </Link>
              }
            >
              <ErrorAlert error={environment.error} focus={false} />
              <KeyValueList items={summary(settings.data, injectedNames)} />
            </Section>
          </Grid>
        </>
      )}
      <Dialog
        open={review}
        onClose={() => setReview(false)}
        title="Deploy this commit?"
        footer={
          <>
            <Button onClick={() => setReview(false)}>Cancel</Button>
            <Button
              variant="primary"
              loading={deploy.isPending}
              disabled={needsIdentity && !identityConfirmed}
              onClick={() => {
                const key = pendingKey ?? crypto.randomUUID();
                setPendingKey(key);
                deploy.mutate(key);
              }}
            >
              Deploy
            </Button>
          </>
        }
      >
        {settings.data && (
          <KeyValueList
            items={[
              {
                label: 'Commit',
                value: <span className="ui-mono ui-break">{sha}</span>,
              },
              ...summary(settings.data, injectedNames).filter((item) =>
                ['Repository', 'Branch', 'Environment variables'].includes(item.label),
              ),
            ]}
          />
        )}
        <Hint>
          Check that this commit is the one you mean: the branch name is only a label. Your app may
          briefly run both versions while the new one starts.
        </Hint>
        {needsIdentity && (
          <Checkbox
            label="Signing in to this portal depends on this app. Deploy it anyway."
            checked={identityConfirmed}
            onChange={(event) => setIdentityConfirmed(event.target.checked)}
          />
        )}
        <ErrorAlert error={deploy.error} />
      </Dialog>
    </AppFrame>
  );
}
