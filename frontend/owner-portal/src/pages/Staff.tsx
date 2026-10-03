import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { Link, Route, Switch, useSearch } from 'wouter';
import { ApiError, type Page } from '../api';
import { BoundaryText } from '../components/BoundaryText';
import { DeploymentRow } from '../components/DeploymentRow';
import { Empty, ErrorNotice, Loading } from '../components/Feedback';
import { Status } from '../components/Status';
import { staffApi, type StaffCatalogApp, type StaffOwner } from '../staffApi';
import { humanPhase, short, time } from '../utils/presentation';

const StaffContext = createContext({ userId: '', active: true });

function useActive() {
  const [active, setActive] = useState(!document.hidden);
  useEffect(() => {
    let interacted = Date.now();
    function interaction() {
      interacted = Date.now();
    }
    function visibility() {
      setActive(!document.hidden);
    }
    for (const event of ['pointerdown', 'keydown', 'scroll'])
      window.addEventListener(event, interaction, { passive: true });
    document.addEventListener('visibilitychange', visibility);
    const timer = window.setInterval(() => {
      if (Date.now() - interacted >= 600000)
        window.dispatchEvent(new Event('portal-session-ended'));
    }, 1000);
    return () => {
      window.clearInterval(timer);
      for (const event of ['pointerdown', 'keydown', 'scroll'])
        window.removeEventListener(event, interaction);
      document.removeEventListener('visibilitychange', visibility);
    };
  }, []);
  return active;
}

function useRead<T>(
  key: (string | undefined)[],
  read: (signal: AbortSignal) => Promise<T>,
  poll = false,
) {
  const { userId, active } = useContext(StaffContext);
  return useQuery({
    queryKey: ['staff', 'staff', userId, ...key],
    queryFn: ({ signal }) => read(signal),
    enabled: active,
    retry: false,
    refetchOnWindowFocus: false,
    refetchInterval: (query) =>
      !active || !poll || query.state.errorUpdateCount >= 3
        ? false
        : query.state.error instanceof ApiError
          ? Math.max(15000, query.state.error.retryAfterSeconds * 1000)
          : 15000,
    refetchIntervalInBackground: false,
  });
}

function Result<T>({
  query,
  children,
}: {
  query: UseQueryResult<T>;
  children: (data: T) => ReactNode;
}) {
  if (query.isPending) return <Loading />;
  if (query.error) return <ErrorNotice error={query.error} />;
  return <>{children(query.data!)}</>;
}
function Pager({
  page,
  cursor,
  setCursor,
}: {
  page: Page<unknown>;
  cursor?: string;
  setCursor: (v?: string) => void;
}) {
  return (
    <div className="pagination">
      {cursor && (
        <button className="button" onClick={() => setCursor(undefined)}>
          Newest records
        </button>
      )}
      {page.nextCursor && (
        <button className="button" onClick={() => setCursor(page.nextCursor!)}>
          Next page →
        </button>
      )}
    </div>
  );
}
function Refresh<T>({ query }: { query: UseQueryResult<T> }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const delay = query.error instanceof ApiError ? query.error.retryAfterSeconds * 1000 : 0;
    const timer = window.setTimeout(
      () => setNow(Date.now()),
      Math.max(0, query.errorUpdatedAt + delay - Date.now()),
    );
    return () => window.clearTimeout(timer);
  }, [query.error, query.errorUpdatedAt]);
  const delayed =
    query.error instanceof ApiError &&
    [429, 503].includes(query.error.status) &&
    now - query.errorUpdatedAt < query.error.retryAfterSeconds * 1000;
  return (
    <button
      className="button button-small"
      onClick={() => void query.refetch()}
      disabled={query.isFetching || delayed}
    >
      Refresh
    </button>
  );
}
function Heading({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="page-heading">
      <div>
        <span className="eyebrow">Staff view · Read only</span>
        <h1>{title}</h1>
      </div>
      {children}
    </div>
  );
}
function Back({ href = '/staff/apps', label = 'Applications' }: { href?: string; label?: string }) {
  return (
    <Link className="back-link" href={href}>
      ← {label}
    </Link>
  );
}

export function StaffOwners() {
  const [cursor, setCursor] = useState<string>();
  const owners = useRead(['owners', cursor], (signal) => staffApi.owners(cursor, signal));
  return (
    <>
      <Heading title="Owners">
        <Refresh query={owners} />
      </Heading>
      <p>Accounts known to the portal, including accounts without applications.</p>
      <section className="card">
        <Result query={owners}>
          {(page) => (
            <>
              {page.items.length ? (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Owner</th>
                        <th>Username</th>
                        <th>Portal account</th>
                      </tr>
                    </thead>
                    <tbody>
                      {page.items.map((owner: StaffOwner) => (
                        <tr key={owner.ownerId}>
                          <td>
                            <Link href={`/staff/owners/${owner.ownerId}`}>{owner.displayName}</Link>
                          </td>
                          <td>{owner.username}</td>
                          <td>{owner.portalEnabled ? 'Enabled' : 'Disabled'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty title="No owners yet">
                  Accounts appear after their first portal sign-in.
                </Empty>
              )}
              <Pager page={page} cursor={cursor} setCursor={setCursor} />
            </>
          )}
        </Result>
      </section>
    </>
  );
}
function AppCatalog({ ownerId }: { ownerId?: string }) {
  const [cursor, setCursor] = useState<string>();
  const apps = useRead(['apps', ownerId, cursor], (signal) =>
    staffApi.apps(ownerId, cursor, signal),
  );
  return (
    <section className="card">
      <div className="card-header">
        <h2>Applications</h2>
        <Refresh query={apps} />
      </div>
      <Result query={apps}>
        {(page) => (
          <>
            {page.items.length ? (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Application</th>
                      <th>Owner</th>
                      <th>Lifecycle</th>
                      <th>Saved revision</th>
                    </tr>
                  </thead>
                  <tbody>
                    {page.items.map((app: StaffCatalogApp) => (
                      <tr key={app.applicationId}>
                        <td>
                          <Link className="app-link" href={`/staff/apps/${app.applicationId}`}>
                            {app.slug}
                          </Link>
                        </td>
                        <td>
                          <Link href={`/staff/owners/${app.ownerId}`}>{short(app.ownerId)}</Link>
                        </td>
                        <td>
                          <Status state={app.lifecycleState} />
                        </td>
                        <td>{app.savedRevision}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty title="No applications">No portal applications match this view.</Empty>
            )}
            <Pager page={page} cursor={cursor} setCursor={setCursor} />
          </>
        )}
      </Result>
    </section>
  );
}
export function StaffApps() {
  const ownerId = new URLSearchParams(useSearch()).get('ownerId') ?? undefined;
  return (
    <>
      <Heading title="Applications" />
      <p>Applications recorded in this portal.</p>
      <AppCatalog key={ownerId ?? 'all'} ownerId={ownerId} />
    </>
  );
}
export function StaffOwnerPage({ id }: { id: string }) {
  const owner = useRead(['owner', id], (signal) => staffApi.owner(id, signal));
  return (
    <>
      <Back href="/staff/owners" label="Owners" />
      <Heading title="Owner" />
      <Result query={owner}>
        {(data) => (
          <section className="card overview-card section">
            <h2>{data.displayName}</h2>
            <p>
              {data.username} · Portal account {data.portalEnabled ? 'enabled' : 'disabled'}
            </p>
            <dl className="overview-meta">
              {Object.entries(data.quota).map(([kind, quota]) => (
                <div key={kind}>
                  <dt>{kind === 'apps' ? 'Application quota' : 'Concurrent deployment quota'}</dt>
                  <dd>
                    {quota.used} used · {quota.reserved} reserved · {quota.limit} limit
                  </dd>
                </div>
              ))}
            </dl>
            <p>
              <Link href={`/staff/operations?ownerId=${id}`}>View operations →</Link>
            </p>
          </section>
        )}
      </Result>
      <AppCatalog key={id} ownerId={id} />
    </>
  );
}
export function StaffAppPage({ id }: { id: string }) {
  const app = useRead(['app', id], (signal) => staffApi.app(id, signal), true);
  return (
    <>
      <Back />
      <Heading title="Application">
        <Refresh query={app} />
      </Heading>
      <Result query={app}>
        {(data) => (
          <>
            <section className="card overview-card section">
              <h2>{data.slug}</h2>
              <p>
                <Link href={`/staff/owners/${data.ownerId}`}>View owner →</Link>
              </p>
              {data.url && (
                <p>
                  <a href={data.url} target="_blank" rel="noopener noreferrer">
                    <BoundaryText text={data.url} />
                  </a>
                </p>
              )}
              <dl className="overview-meta">
                <div>
                  <dt>Repository</dt>
                  <dd>
                    {data.repository ? (
                      <a href={data.repository} target="_blank" rel="noopener noreferrer">
                        <BoundaryText text={data.repository} />
                      </a>
                    ) : (
                      'Not available'
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Lifecycle</dt>
                  <dd>
                    <Status state={data.lifecycleState} />
                  </dd>
                </div>
                <div>
                  <dt>Saved revision</dt>
                  <dd>{data.savedRevision}</dd>
                </div>
                <div>
                  <dt>Accepted commit</dt>
                  <dd>{short(data.acceptedDeployment?.sourceCommit)}</dd>
                </div>
                <div>
                  <dt>Accepted</dt>
                  <dd>{time(data.acceptedDeployment?.acceptedAt)}</dd>
                </div>
                <div>
                  <dt>Application process</dt>
                  <dd>
                    <Status
                      state={data.health.process}
                      label={data.health.process === 'unknown' ? 'Unknown' : undefined}
                    />
                  </dd>
                </div>
                <div>
                  <dt>Public route</dt>
                  <dd>
                    <Status
                      state={data.health.route}
                      label={data.health.route === 'unknown' ? 'Unknown' : undefined}
                    />
                  </dd>
                </div>
              </dl>
              <p className="field-help">
                {data.stale
                  ? 'The latest observation is unavailable or stale.'
                  : `Observed ${time(data.observedAt)}`}
              </p>
            </section>
            <nav className="app-nav" aria-label="Application pages">
              <Link href={`/staff/apps/${id}/deployments`}>Deployments</Link>
              <Link href={`/staff/operations?applicationId=${id}`}>Operations</Link>
            </nav>
          </>
        )}
      </Result>
    </>
  );
}
export function StaffHistory({ id }: { id: string }) {
  const [cursor, setCursor] = useState<string>();
  const history = useRead(['history', id, cursor], (signal) =>
    staffApi.deployments(id, cursor, signal),
  );
  return (
    <>
      <Back href={`/staff/apps/${id}`} label="Application" />
      <Heading title="Deployment history">
        <Refresh query={history} />
      </Heading>
      <section className="card">
        <Result query={history}>
          {(page) => (
            <>
              {page.items.length ? (
                page.items.map((deployment) => (
                  <DeploymentRow
                    key={deployment.deploymentId}
                    id={id}
                    deployment={deployment}
                    href={`/staff/apps/${id}/deployments/${deployment.deploymentId}`}
                  />
                ))
              ) : (
                <Empty title="No deployments">This application has no recorded attempts.</Empty>
              )}
              <Pager page={page} cursor={cursor} setCursor={setCursor} />
            </>
          )}
        </Result>
      </section>
    </>
  );
}
export function StaffDeploymentPage({ id, deployment }: { id: string; deployment: string }) {
  const result = useRead(
    ['deployment', id, deployment],
    (signal) => staffApi.deployment(id, deployment, signal),
    true,
  );
  return (
    <>
      <Back href={`/staff/apps/${id}/deployments`} label="Deployment history" />
      <Heading title="Deployment">
        <Refresh query={result} />
      </Heading>
      <Result query={result}>
        {(data) => (
          <section className="card overview-card section">
            <Status state={data.status} />
            <dl className="overview-meta">
              <div>
                <dt>Commit</dt>
                <dd className="mono">{data.repositoryCommit ?? 'Unknown'}</dd>
              </div>
              <div>
                <dt>Saved revision</dt>
                <dd>{data.configurationRevision ?? 'Unknown'}</dd>
              </div>
              <div>
                <dt>Requested</dt>
                <dd>{time(data.requestedAt)}</dd>
              </div>
              <div>
                <dt>Updated</dt>
                <dd>{time(data.updatedAt)}</dd>
              </div>
              <div>
                <dt>Accepted</dt>
                <dd>{time(data.acceptedAt)}</dd>
              </div>
              <div>
                <dt>Last healthy</dt>
                <dd>{time(data.lastHealthyAt)}</dd>
              </div>
              <div>
                <dt>Cleanup</dt>
                <dd>{data.cleanupState.replaceAll('_', ' ')}</dd>
              </div>
            </dl>
          </section>
        )}
      </Result>
    </>
  );
}
export function StaffOperations() {
  const search = useSearch();
  return <StaffOperationList key={search} search={search} />;
}
function StaffOperationList({ search }: { search: string }) {
  const params = new URLSearchParams(search);
  const ownerId = params.get('ownerId') ?? undefined;
  const applicationId = params.get('applicationId') ?? undefined;
  const [cursor, setCursor] = useState<string>();
  const operations = useRead(
    ['operations', ownerId, applicationId, cursor],
    (signal) => staffApi.operations(ownerId, applicationId, cursor, signal),
    true,
  );
  return (
    <>
      <Heading title="Operations">
        <Refresh query={operations} />
      </Heading>
      <p>Portal activity and its last observed status.</p>
      <section className="card">
        <Result query={operations}>
          {(page) => (
            <>
              {page.items.length ? (
                <ul className="operation-list">
                  {page.items.map((operation) => (
                    <li className="operation" key={operation.intentId}>
                      <div>
                        <strong>{operation.kind.replaceAll('_', ' ')}</strong>
                        <p>
                          <Link href={`/staff/apps/${operation.applicationId}`}>
                            View application →
                          </Link>
                        </p>
                        <p>
                          {humanPhase(operation.stage)} · {time(operation.createdAt)}
                        </p>
                        <p className="muted">
                          Status observed {time(operation.statusObservedAt)} · Cleanup{' '}
                          {operation.cleanupState.replaceAll('_', ' ')}
                        </p>
                        {operation.attention !== 'none' && (
                          <p>{operation.attention.replaceAll('_', ' ')}</p>
                        )}
                      </div>
                      <Status state={operation.state} />
                    </li>
                  ))}
                </ul>
              ) : (
                <Empty title="No operations">No portal activity matches this view.</Empty>
              )}
              <Pager page={page} cursor={cursor} setCursor={setCursor} />
            </>
          )}
        </Result>
      </section>
    </>
  );
}
export function StaffPages({ userId }: { userId: string }) {
  const active = useActive();
  return (
    <StaffContext.Provider value={{ userId, active }}>
      <Switch>
        <Route path="/staff/owners/:id">{(p) => <StaffOwnerPage key={p.id} id={p.id} />}</Route>
        <Route path="/staff/owners">
          <StaffOwners />
        </Route>
        <Route path="/staff/apps/:id/deployments/:deployment">
          {(p) => <StaffDeploymentPage key={p.deployment} id={p.id} deployment={p.deployment} />}
        </Route>
        <Route path="/staff/apps/:id/deployments">
          {(p) => <StaffHistory key={p.id} id={p.id} />}
        </Route>
        <Route path="/staff/apps/:id">{(p) => <StaffAppPage key={p.id} id={p.id} />}</Route>
        <Route path="/staff/apps">
          <StaffApps />
        </Route>
        <Route path="/staff/operations">
          <StaffOperations />
        </Route>
        <Route path="/">
          <StaffOwners />
        </Route>
        <Route>
          <Empty title="Read-only staff session">
            Use the staff navigation, or return to <Link href="/apps">My applications</Link>.
          </Empty>
        </Route>
      </Switch>
    </StaffContext.Provider>
  );
}
