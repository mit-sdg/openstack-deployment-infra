import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, Route, Switch, useLocation } from 'wouter';
import { adminApi } from '../adminApi';
import { adminAppsApi } from '../adminAppsApi';
import { ConfigurationForm } from './Configuration';
import { Empty, ErrorNotice, Loading } from '../components/Feedback';
import { Operation } from '../components/Operation';
import { useIntentPolling } from '../hooks/useIntentPolling';

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
function OwnerField({
  value,
  change,
  label = 'Owner account ID',
}: {
  value: string;
  change: (v: string) => void;
  label?: string;
}) {
  return (
    <div className="field">
      <label htmlFor={label}>{label}</label>
      <input
        id={label}
        value={value}
        onChange={(e) => change(e.target.value)}
        placeholder="Account UUID"
      />
    </div>
  );
}
function ManagedCatalog() {
  const [, navigate] = useLocation();
  const [cursor, setCursor] = useState<string | undefined>();
  const catalog = useQuery({
    queryKey: ['admin', 'apps', cursor],
    queryFn: () => adminAppsApi.list(cursor),
  });
  const [slug, setSlug] = useState('');
  const [owner, setOwner] = useState('');
  const [identifier, setIdentifier] = useState('');
  const [adoptionConfirmed, setAdoptionConfirmed] = useState(false);
  const create = useMutation({
    mutationFn: () => adminAppsApi.create(slug, owner, crypto.randomUUID()),
    onSuccess: (app) => navigate(`/admin/apps/${app.applicationId}`),
  });
  const adopt = useMutation({
    mutationFn: () =>
      adminAppsApi.adopt(identifier, owner || undefined, crypto.randomUUID(), adoptionConfirmed),
    onSuccess: (app) => navigate(`/admin/apps/${app.applicationId}`),
  });
  return (
    <>
      <h1>Manage applications</h1>
      <p>
        All broker applications. Adopt an operator application by its UUID; the project peer cannot
        enumerate unknown applications.
      </p>
      <form
        className="card form-card"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <h2>Create for an owner</h2>
        <div className="field">
          <label htmlFor="managed-slug">Application name</label>
          <input
            id="managed-slug"
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            required
          />
        </div>
        <OwnerField value={owner} change={setOwner} />
        <p>
          Copy an account UUID from Accounts. Adoption defaults to your own account when this field
          is empty.
        </p>
        <button className="button" disabled={create.isPending || !owner}>
          Create application
        </button>
        <ErrorNotice error={create.error} />
      </form>
      <form
        className="card form-card"
        onSubmit={(e) => {
          e.preventDefault();
          adopt.mutate();
        }}
      >
        <h2>Adopt an existing application</h2>
        <div className="field">
          <label htmlFor="adopt-id">Controller application UUID</label>
          <input
            id="adopt-id"
            value={identifier}
            onChange={(e) => setIdentifier(e.target.value)}
            required
          />
        </div>
        <p>
          Imports the current accepted repository, ref and configuration, including storage
          bindings. Adoption does not redeploy the app.
        </p>
        <label>
          <input
            type="checkbox"
            checked={adoptionConfirmed}
            onChange={(e) => setAdoptionConfirmed(e.target.checked)}
          />
          Portal sign-in depends on this app — confirm adoption if this is Commons
        </label>
        <button className="button" disabled={adopt.isPending}>
          Adopt application
        </button>
        <ErrorNotice error={adopt.error} />
      </form>
      <StepUp />
      <ErrorNotice error={catalog.error} />
      {catalog.isPending ? (
        <Loading />
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Application</th>
                <th>Owner account ID</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {catalog.data?.items.map((app) => (
                <tr key={app.applicationId}>
                  <td>
                    <Link href={`/admin/apps/${app.applicationId}`}>{app.slug}</Link>
                  </td>
                  <td>
                    <code>{app.ownerId}</code>
                  </td>
                  <td>{app.lifecycleState}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {catalog.data?.nextCursor && (
        <button className="button" onClick={() => setCursor(catalog.data!.nextCursor!)}>
          Next page
        </button>
      )}
      {cursor && (
        <button className="button" onClick={() => setCursor(undefined)}>
          First page
        </button>
      )}
    </>
  );
}
function StepUp() {
  const [password, setPassword] = useState('');
  const [totp, setTotp] = useState('');
  const proof = useMutation({
    mutationFn: () => adminApi.reauthenticate(password, totp),
    onSettled: () => {
      setPassword('');
      setTotp('');
    },
  });
  return (
    <form
      className="card form-card"
      onSubmit={(e) => {
        e.preventDefault();
        proof.mutate();
      }}
    >
      <h2>Confirm sensitive actions</h2>
      <p>
        Adoption, reassignment and storage deletion require your password and a fresh authentication
        code within five minutes.
      </p>
      <div className="field">
        <label htmlFor="step-password">Admin password</label>
        <input
          id="step-password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </div>
      <div className="field">
        <label htmlFor="step-code">Fresh authentication code</label>
        <input
          id="step-code"
          autoComplete="one-time-code"
          value={totp}
          onChange={(e) => setTotp(e.target.value)}
        />
      </div>
      <button className="button" disabled={proof.isPending}>
        Reauthenticate
      </button>
      {proof.isSuccess && <p role="status">Sensitive actions confirmed for five minutes.</p>}
      <ErrorNotice error={proof.error} />
    </form>
  );
}
function ManagedApplication({ id }: { id: string }) {
  const client = useQueryClient();
  const app = useQuery({ queryKey: ['admin', 'app', id], queryFn: () => adminAppsApi.detail(id) });
  const identity = app.data?.identityProvider === true;
  const service = useMemo(
    () =>
      adminAppsApi.resources(
        () =>
          !identity ||
          window.confirm('Portal sign-in depends on this app. Continue with this storage change?'),
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
  const [owner, setOwner] = useState('');
  const [open, setOpen] = useState(false);
  const [sha, setSha] = useState('');
  const [maintenance, setMaintenance] = useState(false);
  const [plan, setPlan] = useState('');
  const [consent, setConsent] = useState(false);
  const [confirmation, setConfirmation] = useState('');
  const [resource, setResource] = useState('');
  const [intentId, setIntentId] = useState<string | null>(null);
  const operation = useIntentPolling(intentId);
  const refresh = () => {
    client.invalidateQueries({ queryKey: ['admin', 'app', id] });
    client.invalidateQueries({ queryKey: ['admin', 'storage', id] });
    client.invalidateQueries({ queryKey: ['intents'] });
  };
  const deploy = useMutation({
    mutationFn: () =>
      adminAppsApi.deploy(
        id,
        settings.data!.revision,
        sha,
        maintenance,
        plan.trim() ? JSON.parse(plan) : undefined,
        consent,
        crypto.randomUUID(),
      ),
    onSuccess: (result) => {
      setIntentId(result.intentId);
      setOpen(false);
      refresh();
    },
  });
  const reassign = useMutation({
    mutationFn: () =>
      adminAppsApi.reassign(
        id,
        app.data!.ownerId,
        owner,
        identity && window.confirm('Portal sign-in depends on this app. Confirm reassignment?'),
      ),
    onSuccess: refresh,
  });
  const state = useMutation({
    mutationFn: () =>
      adminAppsApi.state(
        id,
        !app.data!.desiredRunning,
        !identity ||
          window.confirm('Portal sign-in depends on this app. Confirm this running-state change?'),
        crypto.randomUUID(),
      ),
    onSuccess: (result) => {
      setIntentId(result.intentId);
      refresh();
    },
  });
  const remove = useMutation({
    mutationFn: () =>
      adminAppsApi.deleteStorage(
        id,
        resource,
        confirmation,
        !identity ||
          window.confirm('Portal sign-in depends on this app. Confirm storage destruction?'),
        crypto.randomUUID(),
      ),
    onSuccess: (result) => {
      setIntentId(result.intentId);
      setResource('');
      setConfirmation('');
      refresh();
    },
  });
  if (app.isPending || settings.isPending) return <Loading />;
  if (app.error || settings.error) return <ErrorNotice error={app.error ?? settings.error} />;
  if (!app.data || !settings.data)
    return <Empty title="Application unavailable">Reload this application.</Empty>;
  return (
    <>
      <Link href="/admin/apps">All managed applications</Link>
      <h1>{app.data.slug}</h1>
      <p>
        Owner: <code>{app.data.ownerId}</code> · {app.data.sizing?.workerFlavor} ·{' '}
        {app.data.sizing?.cpuMHz} MHz / {app.data.sizing?.memoryMiB} MiB
      </p>
      {identity && (
        <p role="status" className="card">
          Portal sign-in depends on this app
        </p>
      )}
      <div className="button-group">
        <button className="button button-primary" onClick={() => setOpen(true)}>
          Deploy application
        </button>
        <button className="button" disabled={state.isPending} onClick={() => state.mutate()}>
          {app.data.desiredRunning ? 'Stop application' : 'Start application'}
        </button>
      </div>
      <ErrorNotice error={state.error} />
      {open && (
        <section className="card form-card" role="dialog" aria-label="Deploy application">
          <h2>Deploy saved revision {settings.data.revision}</h2>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              deploy.mutate();
            }}
          >
            <div className="field">
              <label htmlFor="managed-commit">Full commit SHA</label>
              <input
                id="managed-commit"
                value={sha}
                onChange={(e) => setSha(e.target.value)}
                pattern="[a-f0-9]{40}"
                required
              />
            </div>
            <p>
              {app.data.requiresMaintenance
                ? 'A retained primary IPv4 requires maintenance.'
                : 'Maintenance allows a controlled replacement.'}{' '}
              Brief cutover downtime is expected with maintenance.
            </p>
            <label>
              <input
                type="checkbox"
                checked={maintenance}
                onChange={(e) => setMaintenance(e.target.checked)}
              />
              Allow maintenance cutover
            </label>
            <div className="field">
              <label htmlFor="sizing-plan">Sizing plan JSON (optional)</label>
              <textarea id="sizing-plan" value={plan} onChange={(e) => setPlan(e.target.value)} />
            </div>
            <p>
              Without a plan, the current accepted sizing is preserved. Obtain and review a plan
              through the operator before pasting it here.
            </p>
            {identity && (
              <label>
                <input
                  type="checkbox"
                  checked={consent}
                  onChange={(e) => setConsent(e.target.checked)}
                />
                Portal sign-in depends on this app — confirm deployment
              </label>
            )}
            <ErrorNotice error={deploy.error} />
            <button
              className="button button-primary"
              disabled={
                deploy.isPending ||
                (app.data.requiresMaintenance && !maintenance) ||
                (identity && !consent)
              }
            >
              Confirm deployment
            </button>
            <button className="button" type="button" onClick={() => setOpen(false)}>
              Cancel
            </button>
          </form>
        </section>
      )}
      <ConfigurationForm
        key={id + ':' + settings.data.revision}
        id={id}
        initial={settings.data}
        resources
        service={service}
      />
      <StepUp />
      <form
        className="card form-card"
        onSubmit={(e) => {
          e.preventDefault();
          reassign.mutate();
        }}
      >
        <h2>Reassign ownership</h2>
        <OwnerField value={owner} change={setOwner} label="New owner account ID" />
        <button className="button" disabled={reassign.isPending}>
          Reassign application
        </button>
        <ErrorNotice error={reassign.error} />
      </form>
      <form
        className="card form-card"
        onSubmit={(e) => {
          e.preventDefault();
          remove.mutate();
        }}
      >
        <h2>Delete storage</h2>
        <p role="alert">
          This destroys the selected database or bucket and its data. Nightly platform backups
          exist; recovery requires an operator and may lose newer data. Remove its bindings and save
          configuration before deletion.
        </p>
        <div className="field">
          <label htmlFor="delete-resource">Storage resource</label>
          <select
            id="delete-resource"
            value={resource}
            onChange={(e) => {
              setResource(e.target.value);
              setConfirmation('');
            }}
          >
            <option value="">Choose storage</option>
            {storage.data?.items.map((r) => (
              <option key={r.resourceId} value={r.resourceId}>
                {r.type}: {r.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="storage-confirm">
            Type {app.data.slug}{' '}
            {storage.data?.items.find((r) => r.resourceId === resource)?.type ?? 'storage-type'}
          </label>
          <input
            id="storage-confirm"
            value={confirmation}
            onChange={(e) => setConfirmation(e.target.value)}
          />
        </div>
        <button className="button" disabled={!resource || remove.isPending}>
          Permanently delete storage
        </button>
        <ErrorNotice error={remove.error ?? storage.error} />
      </form>
      {operation.data && (
        <ul className="operation-list">
          <Operation intent={operation.data} />
        </ul>
      )}
    </>
  );
}
