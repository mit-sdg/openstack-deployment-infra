import {
  Alert,
  Button,
  CopyId,
  DataTable,
  Dialog,
  EmptyState,
  ErrorAlert,
  Field,
  Grid,
  Input,
  Page,
  PageHeader,
  RelativeTime,
  Section,
  Select,
  type Column,
} from '@openstack-platform/ui';
import { useState, type FormEvent } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Link, useLocation } from 'wouter';
import { appManagementApi, type CatalogApp } from '../appManagementApi';
import { OperationStatus } from '../components/Operation';
import { Status } from '../components/Status';
import { QueryError } from '../components/Feedback';
import { OwnerPicker, friendly, useDebounced } from './admin/common';
import { Loaded, pager } from '../components/ClassRecords';
import './app-pages.css';

export function catalogRefetchInterval(items?: CatalogApp[]) {
  return items?.some((app) => app.appState === 'unknown' || app.refreshing) ? 3000 : 15000;
}

export function CatalogStatus({ app }: { app: CatalogApp }) {
  return (
    <div className="app-catalog-status ui-stack ui-gap-1">
      <span
        title={
          app.observedAt ? `Last checked ${new Date(app.observedAt).toLocaleString()}` : undefined
        }
      >
        <Status state={app.appState} />
      </span>
      {app.attention.map((intent) => (
        <OperationStatus key={intent.intentId} intent={intent} />
      ))}
    </div>
  );
}

export function AppCatalog({ ownerId, title }: { ownerId?: string; title?: string }) {
  const [, navigate] = useLocation();
  const [search, setSearch] = useState('');
  const q = useDebounced(search.trim());
  const [state, setState] = useState('');
  const [cursor, setCursor] = useState<string>();
  const catalog = useQuery({
    queryKey: ['all-apps', ownerId, q, state, cursor],
    queryFn: () => appManagementApi.list(cursor, q, ownerId, state),
    refetchInterval: (query) => catalogRefetchInterval(query.state.data?.items),
  });
  const columns: Column<CatalogApp>[] = [
    {
      key: 'name',
      header: 'Name',
      mobile: 'title',
      cell: (app) => (
        <Link className="ui-link ui-link--plain" href={`/apps/${app.applicationId}`}>
          {app.slug}
        </Link>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      mobile: 'trailing',
      cell: (app) => <CatalogStatus app={app} />,
    },
    ...(!ownerId
      ? [
          {
            key: 'owner',
            header: 'Owner',
            mobile: 'secondary' as const,
            cell: (app: CatalogApp) => (
              <Link className="ui-link" href={`/people/${app.ownerId}`}>
                {app.ownerDisplayName}
              </Link>
            ),
          },
        ]
      : []),
    {
      key: 'deployed',
      header: 'Last deployed',
      mobile: 'meta',
      cell: (app) => <RelativeTime value={app.lastDeployedAt} empty="Not deployed" />,
    },
    {
      key: 'id',
      header: 'App ID',
      mobile: 'hidden',
      cell: (app) => <CopyId value={app.applicationId} label="app ID" />,
    },
  ];
  return (
    <>
      <Grid columns={2}>
        <Field
          label="Search apps"
          id="app-search"
          hint={ownerId ? 'Search by app name.' : 'Search by app name, owner name or username.'}
        >
          <Input
            type="search"
            value={search}
            maxLength={64}
            onChange={(event) => {
              setSearch(event.target.value);
              setCursor(undefined);
            }}
          />
        </Field>
        <Field label="Status" id="app-status">
          <Select
            value={state}
            onChange={(event) => {
              setState(event.target.value);
              setCursor(undefined);
            }}
          >
            <option value="">Any status</option>
            <option value="attention">Needs attention</option>
            <option value="creating">Creating</option>
            <option value="not_deployed">Not deployed</option>
            <option value="healthy">Healthy</option>
            <option value="unhealthy">Unhealthy</option>
            <option value="stopped">Stopped</option>
            <option value="unknown">Unknown</option>
          </Select>
        </Field>
      </Grid>
      <Section
        className="app-catalog"
        title={title}
        flush
        aria-label={title ?? 'All apps'}
        footer={pager(catalog.data, cursor, setCursor)}
      >
        <Loaded query={catalog} what="apps" rows={5}>
          {(data) => (
            <DataTable
              label={title ?? 'All apps'}
              columns={columns}
              rows={data.items}
              rowKey={(app) => app.applicationId}
              onRowClick={(app) => navigate(`/apps/${app.applicationId}`)}
              empty={
                <EmptyState title={q || state ? 'No matching apps' : 'No apps yet'}>
                  {q || state
                    ? 'Try another search or status.'
                    : 'Apps appear here when they are created or adopted.'}
                </EmptyState>
              }
            />
          )}
        </Loaded>
      </Section>
    </>
  );
}
export function AllAppsPage() {
  const [dialog, setDialog] = useState<'create' | 'adopt' | null>(null);
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
      <AppCatalog />
      <CreateDialog open={dialog === 'create'} onClose={() => setDialog(null)} />
      <AdoptDialog open={dialog === 'adopt'} onClose={() => setDialog(null)} />
    </Page>
  );
}
function CreateDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [, navigate] = useLocation();
  const [slug, setSlug] = useState('');
  const [owner, setOwner] = useState('');
  const create = useMutation({
    mutationFn: () => appManagementApi.create(slug, owner, crypto.randomUUID()),
    onSuccess: (app) => navigate(`/apps/${app.applicationId}`),
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

function AdoptDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [, navigate] = useLocation();
  const [identifier, setIdentifier] = useState('');
  const [owner, setOwner] = useState('');
  const adopt = useMutation({
    mutationFn: () =>
      appManagementApi.adopt(identifier.trim(), owner || undefined, crypto.randomUUID()),
    onSuccess: (app) => navigate(`/apps/${app.applicationId}`),
  });
  const error = friendly(adopt.error);
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
          <Button type="submit" form="admin-adopt-app" variant="primary" loading={adopt.isPending}>
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
          hint="An app already running on the platform. Its settings are imported; it isn’t redeployed."
        >
          <Input
            required
            autoCapitalize="none"
            spellCheck={false}
            className="ui-mono"
            value={identifier}
            onChange={(event) => {
              setIdentifier(event.target.value);
            }}
          />
        </Field>
        <OwnerPicker
          value={owner}
          onChange={setOwner}
          optional
          hint="Leave empty to make yourself the owner."
        />
        <Alert tone="info">
          If this app provides portal sign-in, signing in to this portal depends on it.
        </Alert>
      </form>
    </Dialog>
  );
}
