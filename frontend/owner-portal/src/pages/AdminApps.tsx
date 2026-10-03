import {
  Alert,
  BoundaryText,
  Button,
  Checkbox,
  DataTable,
  Dialog,
  EmptyState,
  ErrorAlert,
  Field,
  Input,
  KeyValueList,
  List,
  ListItem,
  Page,
  PageHeader,
  PageSkeleton,
  Section,
  Select,
  Textarea,
  backLinkClass,
  useToast,
  Icon,
  type Column,
} from '@openstack-platform/ui';
import { useMemo, useState, type FormEvent, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, Route, Switch, useLocation } from 'wouter';
import { ApiError, type StorageResource } from '../api';
import { adminAppsApi, type CatalogApp, type ManagedApp } from '../adminAppsApi';
import { ConfigurationForm } from './Configuration';
import { Operation, OperationList } from '../components/Operation';
import { Status } from '../components/Status';
import { useIntentPolling } from '../hooks/useIntentPolling';
import { healthy, relativeTime, short } from '../utils/presentation';
import {
  AccountName,
  CopyId,
  OwnerPicker,
  friendly,
  shown,
  useAccountNames,
  useStepUp,
} from './admin/common';

const signInWarning = 'This app provides sign-in for the portal';
const storageNames: Record<StorageResource['type'], string> = {
  postgres: 'PostgreSQL',
  mongo: 'MongoDB',
  s3: 'S3 storage',
};

export function AdminAppsPages() {
  return (
    <Switch>
      <Route path="/admin/apps/:id">{(p) => <ManagedApplication id={p.id} />}</Route>
      <Route>
        <ManagedCatalog />
      </Route>
    </Switch>
  );
}

function ManagedCatalog() {
  const [cursor, setCursor] = useState<string | undefined>();
  const [dialog, setDialog] = useState<'create' | 'adopt' | null>(null);
  const catalog = useQuery({
    queryKey: ['admin', 'apps', cursor],
    queryFn: () => adminAppsApi.list(cursor),
  });
  const names = useAccountNames();
  const stepUp = useStepUp();
  const columns: Column<CatalogApp>[] = [
    {
      key: 'name',
      header: 'Name',
      mobile: 'title',
      cell: (app) => (
        <Link href={`/admin/apps/${app.applicationId}`} className="ui-link ui-link--plain">
          {app.slug}
        </Link>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      mobile: 'trailing',
      cell: (app) => <Status state={app.lifecycleState} />,
    },
    {
      key: 'owner',
      header: 'Owner',
      cell: (app) => <AccountName id={app.ownerId} names={names} label="owner ID" />,
    },
    {
      key: 'id',
      header: 'App ID',
      cell: (app) => <CopyId value={app.applicationId} label="app ID" />,
    },
  ];
  if (catalog.isPending) return <PageSkeleton />;
  return (
    <Page>
      <PageHeader
        title="All apps"
        actions={
          <>
            <Button onClick={() => setDialog('adopt')}>Adopt app</Button>
            <Button variant="primary" icon="plus" onClick={() => setDialog('create')}>
              Create app
            </Button>
          </>
        }
      />
      <ErrorAlert error={catalog.error} />
      {catalog.data &&
        (catalog.data.items.length || cursor ? (
          <Section
            flush
            aria-label="All apps"
            footer={
              (cursor || catalog.data.nextCursor) && (
                <>
                  {cursor && (
                    <Button size="sm" variant="ghost" onClick={() => setCursor(undefined)}>
                      First page
                    </Button>
                  )}
                  {catalog.data.nextCursor && (
                    <Button size="sm" onClick={() => setCursor(catalog.data!.nextCursor!)}>
                      Next page
                    </Button>
                  )}
                </>
              )
            }
          >
            <DataTable
              label="All apps"
              columns={columns}
              rows={catalog.data.items}
              rowKey={(app) => app.applicationId}
            />
          </Section>
        ) : (
          <div className="ui-card">
            <EmptyState title="No apps yet">
              Apps appear here when owners create them, or when you create or adopt one.
            </EmptyState>
          </div>
        ))}
      <CreateDialog open={dialog === 'create'} onClose={() => setDialog(null)} />
      <AdoptDialog open={dialog === 'adopt'} onClose={() => setDialog(null)} run={stepUp.run} />
      {stepUp.dialog}
    </Page>
  );
}

function CreateDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [, navigate] = useLocation();
  const [slug, setSlug] = useState('');
  const [owner, setOwner] = useState('');
  const create = useMutation({
    mutationFn: () => adminAppsApi.create(slug, owner, crypto.randomUUID()),
    onSuccess: (app) => navigate(`/admin/apps/${app.applicationId}`),
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate();
  }
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Create app"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            type="submit"
            form="admin-create-app"
            variant="primary"
            loading={create.isPending}
            disabled={!owner}
          >
            Create app
          </Button>
        </>
      }
    >
      <form id="admin-create-app" className="ui-stack ui-gap-4" onSubmit={submit}>
        <ErrorAlert error={friendly(create.error)} />
        <Field label="App name" id="managed-slug">
          <Input
            required
            autoCapitalize="none"
            spellCheck={false}
            value={slug}
            onChange={(event) => setSlug(event.target.value)}
          />
        </Field>
        <OwnerPicker value={owner} onChange={setOwner} />
      </form>
    </Dialog>
  );
}

function AdoptDialog({
  open,
  onClose,
  run,
}: {
  open: boolean;
  onClose: () => void;
  run: ReturnType<typeof useStepUp>['run'];
}) {
  const [, navigate] = useLocation();
  const [identifier, setIdentifier] = useState('');
  const [owner, setOwner] = useState('');
  const [needsConsent, setNeedsConsent] = useState(false);
  const [consent, setConsent] = useState(false);
  const adopt = useMutation({
    mutationFn: () =>
      run(() =>
        adminAppsApi.adopt(identifier.trim(), owner || undefined, crypto.randomUUID(), consent),
      ),
    onSuccess: (app) => navigate(`/admin/apps/${app.applicationId}`),
    onError: (error) => {
      if (error instanceof ApiError && error.code === 'IDENTITY_CONFIRMATION_REQUIRED')
        setNeedsConsent(true);
    },
  });
  const error =
    adopt.error instanceof ApiError && adopt.error.code === 'IDENTITY_CONFIRMATION_REQUIRED'
      ? null
      : friendly(adopt.error);
  function submit(event: FormEvent) {
    event.preventDefault();
    adopt.mutate();
  }
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Adopt app"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            type="submit"
            form="admin-adopt-app"
            variant="primary"
            loading={adopt.isPending}
            disabled={needsConsent && !consent}
          >
            Adopt app
          </Button>
        </>
      }
    >
      <form id="admin-adopt-app" className="ui-stack ui-gap-4" onSubmit={submit}>
        <ErrorAlert error={error} />
        <Field
          label="App ID"
          id="adopt-id"
          hint="For an app that already runs on the platform. Its repository and settings are imported, and it isn’t redeployed."
        >
          <Input
            required
            autoCapitalize="none"
            spellCheck={false}
            className="ui-mono"
            placeholder="00000000-0000-0000-0000-000000000000"
            value={identifier}
            onChange={(event) => {
              setIdentifier(event.target.value);
              setNeedsConsent(false);
              setConsent(false);
            }}
          />
        </Field>
        <OwnerPicker
          value={owner}
          onChange={setOwner}
          optional
          hint="Leave empty to make yourself the owner."
        />
        {needsConsent && (
          <SignInConsent checked={consent} onChange={setConsent} label="Adopt the sign-in app" />
        )}
      </form>
    </Dialog>
  );
}

/** Warning and required checkbox before changing the app that provides sign-in. */
function SignInConsent({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
}) {
  return (
    <>
      <Alert tone="warning" title={signInWarning}>
        If this change goes wrong, nobody can sign in to the portal.
      </Alert>
      <Checkbox
        label={label}
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
      />
    </>
  );
}

function size(app: ManagedApp) {
  if (!app.sizing) return null;
  const memory =
    app.sizing.memoryMiB % 1024 === 0
      ? `${app.sizing.memoryMiB / 1024} GB`
      : `${app.sizing.memoryMiB} MB`;
  return `${(app.sizing.cpuMHz / 1000).toLocaleString()} GHz CPU · ${memory} memory`;
}

type Action = 'deploy' | 'state' | 'owner' | 'storage' | null;

function ManagedApplication({ id }: { id: string }) {
  const client = useQueryClient();
  const toast = useToast();
  const app = useQuery({ queryKey: ['admin', 'app', id], queryFn: () => adminAppsApi.detail(id) });
  const identity = app.data?.identityProvider === true;
  const service = useMemo(
    () =>
      adminAppsApi.resources(
        () => !identity || window.confirm(`${signInWarning}. Continue with this storage change?`),
      ),
    [id, identity],
  );
  const settings = useQuery({
    queryKey: ['admin', 'settings', id],
    queryFn: () => service.settings(id),
  });
  const storage = useQuery({
    queryKey: ['admin', 'storage', id],
    queryFn: () => service.storage(id),
  });
  const names = useAccountNames();
  const stepUp = useStepUp();
  const [action, setAction] = useState<Action>(null);
  const [intentId, setIntentId] = useState<string | null>(null);
  const operation = useIntentPolling(intentId);
  const refresh = () => {
    client.invalidateQueries({ queryKey: ['admin', 'app', id] });
    client.invalidateQueries({ queryKey: ['admin', 'storage', id] });
    client.invalidateQueries({ queryKey: ['intents'] });
  };
  const started = (result: { intentId: string }) => {
    setIntentId(result.intentId);
    setAction(null);
    refresh();
  };
  if (app.isPending || settings.isPending) return <PageSkeleton />;
  const back = (
    <Link href="/admin/apps" className={backLinkClass}>
      <Icon name="arrow-left" />
      All apps
    </Link>
  );
  if (app.error || settings.error) {
    const missing = app.error instanceof ApiError && app.error.status === 404;
    return (
      <Page>
        <PageHeader title={missing ? 'App not found' : 'App unavailable'} back={back} />
        {missing ? (
          <div className="ui-card">
            <EmptyState title="This app isn’t managed here" icon="search">
              Check the address, or adopt the app from All apps.
            </EmptyState>
          </div>
        ) : (
          <ErrorAlert error={app.error ?? settings.error} />
        )}
      </Page>
    );
  }
  const data = app.data;
  const resources = storage.data?.items ?? [];
  return (
    <Page>
      <PageHeader
        title={data.slug}
        back={back}
        meta={<Status state={data.lifecycleState === 'creating' ? 'creating' : healthy(data)} />}
        actions={
          <>
            <Button onClick={() => setAction('state')}>
              {data.desiredRunning ? 'Stop app' : 'Start app'}
            </Button>
            <Button variant="primary" onClick={() => setAction('deploy')}>
              Deploy
            </Button>
          </>
        }
      />
      {identity && (
        <Alert tone="warning" title={signInWarning}>
          Changes here can stop everyone from signing in. You’ll be asked to confirm each one.
        </Alert>
      )}
      <Section title="Details">
        <KeyValueList
          columns={2}
          items={[
            {
              label: 'Owner',
              value: <AccountName id={data.ownerId} names={names} label="owner ID" />,
            },
            {
              label: 'URL',
              value: data.url ? (
                <a className="ui-link" href={data.url} target="_blank" rel="noopener noreferrer">
                  <BoundaryText text={new URL(data.url).hostname} />
                </a>
              ) : (
                <span className="ui-text-subtle">—</span>
              ),
            },
            {
              label: 'Last deployed',
              value: data.acceptedDeployment ? (
                <span>
                  <code className="ui-mono">{short(data.acceptedDeployment.sourceCommit)}</code>
                  {' · '}
                  <time
                    className="ui-text-muted"
                    dateTime={data.acceptedDeployment.acceptedAt}
                    title={new Date(data.acceptedDeployment.acceptedAt).toLocaleString()}
                  >
                    {relativeTime(data.acceptedDeployment.acceptedAt)}
                  </time>
                </span>
              ) : (
                <span className="ui-text-subtle">Not deployed</span>
              ),
            },
            { label: 'Size', value: size(data) ?? <span className="ui-text-subtle">—</span> },
            { label: 'App ID', value: <CopyId value={data.applicationId} label="app ID" /> },
          ]}
        />
      </Section>
      <ErrorAlert error={operation.error} focus={false} />
      {operation.data && (
        <Section title="Latest change" flush>
          <OperationList label="Latest change">
            <Operation intent={operation.data} showApp={false} />
          </OperationList>
        </Section>
      )}
      <ConfigurationForm
        key={id + ':' + settings.data.revision}
        id={id}
        initial={settings.data}
        resources
        service={service}
      />
      <Section title="Danger zone" flush>
        <List label="Danger zone">
          <ListItem
            title="Change owner"
            meta="Move this app to another account."
            trailing={
              <Button size="sm" onClick={() => setAction('owner')}>
                Change owner
              </Button>
            }
          />
          <ListItem
            title="Delete a database or storage"
            meta={
              resources.length
                ? 'Permanently delete it and all of its data.'
                : 'This app has no databases or storage.'
            }
            trailing={
              <Button
                size="sm"
                variant="danger"
                disabled={!resources.length}
                onClick={() => setAction('storage')}
              >
                Delete
              </Button>
            }
          />
        </List>
      </Section>
      <DeployDialog
        open={action === 'deploy'}
        onClose={() => setAction(null)}
        app={data}
        revision={settings.data.revision}
        onStarted={started}
      />
      <StateDialog
        open={action === 'state'}
        onClose={() => setAction(null)}
        app={data}
        onStarted={started}
      />
      <OwnerDialog
        open={action === 'owner'}
        onClose={() => setAction(null)}
        app={data}
        run={stepUp.run}
        onDone={() => {
          setAction(null);
          refresh();
          toast('Owner changed');
        }}
      />
      <StorageDialog
        open={action === 'storage'}
        onClose={() => setAction(null)}
        app={data}
        resources={resources}
        run={stepUp.run}
        onStarted={started}
      />
      {stepUp.dialog}
    </Page>
  );
}

function ActionDialog({
  open,
  onClose,
  title,
  form,
  submit,
  pending,
  disabled = false,
  danger = false,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  form: string;
  submit: string;
  pending: boolean;
  disabled?: boolean;
  danger?: boolean;
  children: ReactNode;
}) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={title}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            type="submit"
            form={form}
            variant={danger ? 'danger' : 'primary'}
            loading={pending}
            disabled={disabled}
          >
            {submit}
          </Button>
        </>
      }
    >
      {children}
    </Dialog>
  );
}

function DeployDialog({
  open,
  onClose,
  app,
  revision,
  onStarted,
}: {
  open: boolean;
  onClose: () => void;
  app: ManagedApp;
  revision: number;
  onStarted: (result: { intentId: string }) => void;
}) {
  const identity = app.identityProvider;
  const [sha, setSha] = useState('');
  const [maintenance, setMaintenance] = useState(false);
  const [plan, setPlan] = useState('');
  const [planError, setPlanError] = useState<string | null>(null);
  const [consent, setConsent] = useState(false);
  const deploy = useMutation({
    mutationFn: (parsed: unknown) =>
      adminAppsApi.deploy(
        app.applicationId,
        revision,
        sha,
        maintenance,
        parsed,
        consent,
        crypto.randomUUID(),
      ),
    onSuccess: onStarted,
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    let parsed: unknown;
    if (plan.trim()) {
      try {
        parsed = JSON.parse(plan);
      } catch {
        setPlanError('Enter valid JSON, or leave this empty.');
        return;
      }
    }
    setPlanError(null);
    deploy.mutate(parsed);
  }
  return (
    <ActionDialog
      open={open}
      onClose={onClose}
      title={`Deploy ${app.slug}`}
      form="admin-deploy"
      submit="Deploy"
      pending={deploy.isPending}
      disabled={(app.requiresMaintenance && !maintenance) || (identity && !consent)}
    >
      <form id="admin-deploy" className="ui-stack ui-gap-4" onSubmit={submit}>
        <ErrorAlert error={deploy.error} />
        <Field label="Commit" id="managed-commit" hint="The full 40-character commit SHA.">
          <Input
            required
            pattern="[a-f0-9]{40}"
            autoCapitalize="none"
            spellCheck={false}
            className="ui-mono"
            value={sha}
            onChange={(event) => setSha(event.target.value)}
          />
        </Field>
        <Checkbox
          label="Allow a brief outage"
          description={
            app.requiresMaintenance
              ? 'Required: this app keeps a fixed IP address, so it goes offline briefly while the new version starts.'
              : 'Replace the running app in one step. It goes offline briefly while the new version starts.'
          }
          checked={maintenance}
          onChange={(event) => setMaintenance(event.target.checked)}
        />
        <Field
          label="Sizing plan"
          id="sizing-plan"
          optional
          error={planError}
          hint="Paste a reviewed plan as JSON. Leave empty to keep the current size."
        >
          <Textarea
            rows={3}
            spellCheck={false}
            className="ui-mono"
            value={plan}
            onChange={(event) => {
              setPlan(event.target.value);
              setPlanError(null);
            }}
          />
        </Field>
        {identity && (
          <SignInConsent checked={consent} onChange={setConsent} label="Deploy the sign-in app" />
        )}
      </form>
    </ActionDialog>
  );
}

function StateDialog({
  open,
  onClose,
  app,
  onStarted,
}: {
  open: boolean;
  onClose: () => void;
  app: ManagedApp;
  onStarted: (result: { intentId: string }) => void;
}) {
  const identity = app.identityProvider;
  const stopping = app.desiredRunning;
  const [consent, setConsent] = useState(false);
  const state = useMutation({
    mutationFn: () =>
      adminAppsApi.state(app.applicationId, !stopping, !identity || consent, crypto.randomUUID()),
    onSuccess: onStarted,
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    state.mutate();
  }
  return (
    <ActionDialog
      open={open}
      onClose={onClose}
      title={stopping ? `Stop ${app.slug}?` : `Start ${app.slug}?`}
      form="admin-state"
      submit={stopping ? 'Stop app' : 'Start app'}
      danger={stopping}
      pending={state.isPending}
      disabled={identity && !consent}
    >
      <form id="admin-state" className="ui-stack ui-gap-4" onSubmit={submit}>
        <ErrorAlert error={state.error} />
        <p>
          {stopping
            ? 'The app goes offline until someone starts it again. Its settings and data are kept.'
            : 'The app starts with its last deployed version.'}
        </p>
        {identity && (
          <SignInConsent
            checked={consent}
            onChange={setConsent}
            label={stopping ? 'Stop the sign-in app' : 'Start the sign-in app'}
          />
        )}
      </form>
    </ActionDialog>
  );
}

function OwnerDialog({
  open,
  onClose,
  app,
  run,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  app: ManagedApp;
  run: ReturnType<typeof useStepUp>['run'];
  onDone: () => void;
}) {
  const identity = app.identityProvider;
  const [owner, setOwner] = useState('');
  const [consent, setConsent] = useState(false);
  const reassign = useMutation({
    mutationFn: () =>
      run(() => adminAppsApi.reassign(app.applicationId, app.ownerId, owner, identity && consent)),
    onSuccess: onDone,
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    reassign.mutate();
  }
  return (
    <ActionDialog
      open={open}
      onClose={onClose}
      title="Change owner"
      form="admin-owner"
      submit="Change owner"
      pending={reassign.isPending}
      disabled={!owner || owner === app.ownerId || (identity && !consent)}
    >
      <form id="admin-owner" className="ui-stack ui-gap-4" onSubmit={submit}>
        <ErrorAlert error={friendly(reassign.error)} />
        <OwnerPicker value={owner} onChange={setOwner} />
        {identity && (
          <SignInConsent
            checked={consent}
            onChange={setConsent}
            label="Change the sign-in app’s owner"
          />
        )}
      </form>
    </ActionDialog>
  );
}

function StorageDialog({
  open,
  onClose,
  app,
  resources,
  run,
  onStarted,
}: {
  open: boolean;
  onClose: () => void;
  app: ManagedApp;
  resources: StorageResource[];
  run: ReturnType<typeof useStepUp>['run'];
  onStarted: (result: { intentId: string }) => void;
}) {
  const identity = app.identityProvider;
  const [resource, setResource] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [consent, setConsent] = useState(false);
  const chosen = resources.find((r) => r.resourceId === resource);
  const phrase = chosen ? `${app.slug} ${chosen.type}` : '';
  const remove = useMutation({
    mutationFn: () =>
      run(() =>
        adminAppsApi.deleteStorage(
          app.applicationId,
          resource,
          confirmation,
          !identity || consent,
          crypto.randomUUID(),
        ),
      ),
    onSuccess: (result) => {
      setResource('');
      setConfirmation('');
      onStarted(result);
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    remove.mutate();
  }
  return (
    <ActionDialog
      open={open}
      onClose={onClose}
      title="Delete a database or storage"
      form="admin-storage"
      submit="Delete permanently"
      danger
      pending={remove.isPending}
      disabled={!chosen || confirmation !== phrase || (identity && !consent)}
    >
      <form id="admin-storage" className="ui-stack ui-gap-4" onSubmit={submit}>
        <ErrorAlert error={shown(remove.error)} />
        <Alert tone="danger" title="This can’t be undone">
          All data is deleted. Nightly backups exist, but restoring one needs help from the platform
          team and can lose recent changes. Remove it from the app’s settings and save before you
          delete it.
        </Alert>
        <Field label="Database or storage" id="delete-resource">
          <Select
            required
            value={resource}
            onChange={(event) => {
              setResource(event.target.value);
              setConfirmation('');
            }}
          >
            <option value="">Choose one</option>
            {resources.map((r) => (
              <option key={r.resourceId} value={r.resourceId}>
                {storageNames[r.type] ?? r.type} · {r.label}
              </option>
            ))}
          </Select>
        </Field>
        {chosen && (
          <Field label={`Type “${phrase}” to confirm`} id="storage-confirm">
            <Input
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              value={confirmation}
              onChange={(event) => setConfirmation(event.target.value)}
            />
          </Field>
        )}
        {identity && (
          <SignInConsent
            checked={consent}
            onChange={setConsent}
            label="Delete storage from the sign-in app"
          />
        )}
      </form>
    </ActionDialog>
  );
}
