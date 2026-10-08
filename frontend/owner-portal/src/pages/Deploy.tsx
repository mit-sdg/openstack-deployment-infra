import {
  Alert,
  Button,
  Dialog,
  EmptyState,
  ErrorAlert,
  Field,
  Grid,
  Hint,
  Input,
  Select,
  KeyValueList,
  PageSkeleton,
  Section,
  SectionSkeleton,
  buttonClass,
} from '@openstack-platform/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState, type FormEvent } from 'react';
import { Link, useSearch } from 'wouter';
import { request, intentData, api, type Settings } from '../api';
import { AppFrame } from '../components/AppFrame';
import { BoundaryText } from '../components/BoundaryText';
import { QueryError } from '../components/Feedback';
import { Operation, OperationList } from '../components/Operation';
import { CommitChecks, CommitProblems, useCommitChecks } from '../components/CommitChecks';
import { RecentCommits, useRecentCommits } from '../components/RecentCommits';
import { recentCommits } from '../utils/github';
import { useIntentPolling } from '../hooks/useIntentPolling';
import {
  buildMachineLabel,
  sizingApi,
  workerSizeLabel,
  type Flavor,
  type ResizePlan,
} from '../sizingApi';
import { SizeOptions } from '../components/SizeOptions';

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

function SizeSummary({
  plan,
  current,
  maintenance,
}: {
  plan: ResizePlan;
  current?: Flavor;
  maintenance: boolean;
}) {
  const memory =
    plan.flavor.ram_mib -
    Math.max(
      plan.reserve.memoryMiBMinimum,
      Math.ceil((plan.flavor.ram_mib * plan.reserve.percentMinimum) / 100),
    );
  return (
    <Alert tone="info">
      <KeyValueList
        items={[
          { label: 'Now', value: current ? workerSizeLabel(current) : plan.current.flavor },
          { label: 'After deploy', value: workerSizeLabel(plan.flavor) },
        ]}
      />
      <p>
        The app can use up to {(memory / 1024).toFixed(1)} GB of memory and most of{' '}
        {plan.flavor.vcpus} {plan.flavor.vcpus === 1 ? 'vCPU' : 'vCPUs'}; a little is kept for the
        system.
      </p>
      <p>Deploying with a new size starts the app on a new worker.</p>
      {maintenance && (
        <p>
          This app keeps a fixed IP address, so it goes offline briefly while the new worker starts.
        </p>
      )}
    </Alert>
  );
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
  const search = new URLSearchParams(useSearch());
  const selected = search.get('commit') ?? '';
  const latest = search.get('latest') === '1';
  const [sha, setSha] = useState(/^[a-f0-9]{40}$/.test(selected) ? selected : '');
  const platform = { id, revision: settings.data?.revision ?? 0, service: api };
  const recent = useRecentCommits(
    settings.data?.repository ?? '',
    settings.data?.branch ?? '',
    platform,
  );
  const picked = recent.data?.find((commit) => commit.sha === sha);
  const checks = useCommitChecks(
    settings.data?.repository ?? '',
    sha,
    settings.data?.configuration,
    platform,
  );
  const [error, setError] = useState<string | null>(null);
  const [maintenance, setMaintenance] = useState(false);
  const [size, setSize] = useState('');
  const elevated = identity.data?.access === 'admin';
  const sizes = useQuery({
    queryKey: ['sizes', id],
    queryFn: () => sizingApi.sizes(id),
    enabled: elevated,
  });
  const plan = useQuery({
    queryKey: ['resize-plan', id, size],
    queryFn: () => sizingApi.plan(id, size),
    enabled: elevated && !!size,
    staleTime: 0,
  });
  const sizeReady = !size || (!plan.isFetching && !!plan.data && !plan.error);
  const currentSize = sizes.data?.find((item) => item.name === identity.data?.sizing?.workerFlavor);
  const builder = useQuery({
    queryKey: ['builder-size', id],
    queryFn: () => sizingApi.builder(id),
    enabled: elevated,
  });
  const usesMaintenance = elevated && (identity.data?.requiresMaintenance === true || maintenance);

  const [intentId, setIntentId] = useState<string | null>(null);
  const [review, setReview] = useState(false);
  const [findingLatest, setFindingLatest] = useState(false);
  const [latestError, setLatestError] = useState<string | null>(null);
  const repository = settings.data?.repository;
  const branch = settings.data?.branch;
  useEffect(() => {
    if (!latest || !repository || !branch) return;
    const abort = new AbortController();
    setFindingLatest(true);
    setLatestError(null);
    async function find() {
      try {
        let commit: string | null = null;
        try {
          commit = (await recentCommits(repository!, branch!, abort.signal))[0]?.sha ?? null;
        } catch {
          if (abort.signal.aborted) return;
          if ((await api.sourceKey(id)).present)
            commit = (await api.recentSourceCommits(id))[0]?.sha ?? null;
        }
        if (abort.signal.aborted) return;
        if (!commit) throw new Error('missing latest commit');
        setSha(commit);
        setReview(true);
      } catch {
        if (!abort.signal.aborted)
          setLatestError(
            'Couldn’t find the latest commit. Check repository access in Settings, or pick a commit below.',
          );
      } finally {
        if (!abort.signal.aborted) setFindingLatest(false);
      }
    }
    void find();
    return () => abort.abort();
  }, [latest, repository, branch, id]);
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const client = useQueryClient();
  const intent = useIntentPolling(intentId);
  const deploy = useMutation({
    mutationFn: (key: string) => {
      if (identity.data?.access !== 'admin')
        return api.deploy(id, settings.data!.revision, sha, key);
      if (!sizeReady) throw new Error('Wait for the size details, or keep the current size.');
      return request(`/apps/${id}/deployments`, intentData, {
        method: 'POST',
        key,
        body: {
          configurationRevision: settings.data!.revision,
          commit: sha,
          maintenance: usesMaintenance,
          ...(size && plan.data ? { plan: plan.data } : {}),
        },
      });
    },
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
        <PageSkeleton label="Loading deploy…">
          <Grid columns={2} gap={6} className="app-deploy">
            <SectionSkeleton title rows={2} />
            <SectionSkeleton title rows={6} />
          </Grid>
        </PageSkeleton>
      ) : settings.error ? (
        <QueryError query={settings} what="your settings" />
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
          {findingLatest && <Hint>Finding the latest commit on {branch}…</Hint>}
          {latestError && <Alert tone="warning">{latestError}</Alert>}
          {selected && (
            <Alert tone="info">
              Deploying this commit again uses your app’s current saved settings and environment
              variables.
            </Alert>
          )}
          <Grid columns={2} gap={6} className="app-deploy">
            <form onSubmit={submit} noValidate>
              <Section
                title="Deploy a commit"
                footer={
                  <Button
                    type="submit"
                    variant="primary"
                    disabled={deploy.isPending || findingLatest || !sizeReady}
                  >
                    Review deployment
                  </Button>
                }
              >
                <RecentCommits
                  repository={settings.data.repository}
                  branch={settings.data.branch}
                  platform={platform}
                  value={sha}
                  onSelect={(commit) => {
                    setSha(commit.sha);
                    setError(null);
                    setPendingKey(null);
                  }}
                />
                <Field
                  label="Commit SHA"
                  id="commit"
                  error={error}
                  hint="Pick a commit above or paste a full SHA from GitHub. The deployment always uses this exact commit, even if the branch moves on."
                >
                  <Input
                    className="app-mono-input"
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
                {identity.data?.access === 'admin' && (
                  <>
                    {identity.data.requiresMaintenance ? (
                      !size && (
                        <Alert tone="info">
                          This app keeps a fixed IP address, so it goes offline briefly while the
                          new worker starts.
                        </Alert>
                      )
                    ) : (
                      <Field
                        label="Deployment method"
                        id="deployment-method"
                        hint="Replacing the running app takes it offline briefly."
                      >
                        <Select
                          value={maintenance ? 'replace' : 'alongside'}
                          onChange={(event) => {
                            setMaintenance(event.target.value === 'replace');
                            setPendingKey(null);
                          }}
                        >
                          <option value="alongside">Start alongside the running app</option>
                          <option value="replace">Replace the running app in one step</option>
                        </Select>
                      </Field>
                    )}
                    {sizes.error && <QueryError query={sizes} what="the available sizes" />}
                    <Field label="Worker size" id="worker-size">
                      <Select
                        className="app-size-select"
                        autoFocus={window.location.hash === '#worker-size'}
                        value={size}
                        onChange={(event) => {
                          setSize(event.target.value);
                          setPendingKey(null);
                        }}
                      >
                        <option value="">
                          Keep current size
                          {currentSize
                            ? ` — ${workerSizeLabel(currentSize)}`
                            : identity.data.sizing
                              ? ` — ${identity.data.sizing.workerFlavor}`
                              : ''}
                        </option>
                        <SizeOptions
                          sizes={sizes.data ?? []}
                          current={currentSize}
                          disableCurrent
                        />
                      </Select>
                    </Field>
                    {size && plan.isFetching && <Hint>Checking the selected size…</Hint>}
                    {size && plan.error && <QueryError query={plan} what="the selected size" />}
                    {size && plan.data && !plan.isFetching && (
                      <SizeSummary
                        plan={plan.data}
                        current={currentSize}
                        maintenance={identity.data.requiresMaintenance === true}
                      />
                    )}
                  </>
                )}
                <CommitChecks
                  repository={settings.data.repository}
                  sha={sha}
                  configuration={settings.data.configuration}
                  platform={platform}
                />
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
              {environment.error && (
                <QueryError query={environment} what="your environment variables" />
              )}
              {elevated && builder.error && <QueryError query={builder} what="the build machine" />}
              <KeyValueList
                items={[
                  ...summary(settings.data, injectedNames),
                  ...(elevated && builder.data
                    ? [{ label: 'Build machine', value: buildMachineLabel(builder.data) }]
                    : []),
                ]}
              />
            </Section>
          </Grid>
        </>
      )}
      <Dialog
        open={review}
        onClose={() => setReview(false)}
        size="lg"
        title="Deploy this commit?"
        footer={
          <>
            <Button onClick={() => setReview(false)}>Cancel</Button>
            <Button
              variant="primary"
              loading={deploy.isPending}
              disabled={!sizeReady}
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
                stacked: true,
              },
              ...(picked ? [{ label: 'Message', value: picked.message }] : []),
              ...summary(settings.data, injectedNames).filter((item) =>
                ['Repository', 'Branch', 'Environment variables'].includes(item.label),
              ),
            ]}
          />
        )}
        {size && plan.data && (
          <SizeSummary
            plan={plan.data}
            current={currentSize}
            maintenance={identity.data?.requiresMaintenance === true}
          />
        )}
        <CommitProblems checks={checks.data} />
        <Hint>
          This deploy uses your app’s current saved settings and environment variables. Check that
          this commit is the one you mean: the branch name is only a label.{' '}
          {usesMaintenance
            ? 'The app goes offline briefly while the new worker starts.'
            : 'Your app may briefly run both versions while the new one starts.'}
        </Hint>
        {needsIdentity && <Alert tone="info">Signing in to this portal depends on this app.</Alert>}
        <ErrorAlert error={deploy.error} />
      </Dialog>
    </AppFrame>
  );
}
