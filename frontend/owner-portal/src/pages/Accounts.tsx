import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { adminApi, type Account } from '../adminApi';
import { ErrorNotice, Loading } from '../components/Feedback';
import { time } from '../utils/presentation';

export function Reauthenticate() {
  const [password, setPassword] = useState('');
  const [totp, setTotp] = useState('');
  const client = useQueryClient();
  const step = useMutation({
    mutationFn: async () => {
      try {
        return await adminApi.reauthenticate(password, totp);
      } finally {
        setPassword('');
        setTotp('');
      }
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['session'] });
    },
  });
  return (
    <details className="card overview-card">
      <summary>Confirm a sensitive account action</summary>
      <p>
        Re-enter your password and a fresh authentication code. Confirmation lasts five minutes.
      </p>
      <ErrorNotice error={step.error} />
      {step.isSuccess && <p role="status">Account actions confirmed for five minutes.</p>}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          step.mutate();
        }}
      >
        <div className="field">
          <label htmlFor="step-password">Admin password</label>
          <input
            id="step-password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </div>
        <div className="field">
          <label htmlFor="step-code">Fresh authentication code</label>
          <input
            id="step-code"
            autoComplete="one-time-code"
            inputMode="numeric"
            maxLength={6}
            value={totp}
            onChange={(e) => setTotp(e.target.value)}
            required
          />
        </div>
        <button className="button" disabled={step.isPending}>
          Confirm identity
        </button>
      </form>
    </details>
  );
}
function OneTimeLink({ value }: { value: string }) {
  return (
    <div className="notice">
      <p>
        Copy and share this single-use link privately. It expires after 72 hours. The portal sends
        no email.
      </p>
      <textarea
        aria-label="Account setup link"
        readOnly
        value={value}
        onFocus={(e) => e.target.select()}
      />
    </div>
  );
}
function AccountControls({ account }: { account: Account }) {
  const [role, setRole] = useState(account.role);
  const [apps, setApps] = useState(account.appLimit);
  const [concurrent, setConcurrent] = useState(account.concurrencyLimit);
  const [link, setLink] = useState('');
  const client = useQueryClient();
  const change = useMutation({
    mutationFn: ({ action, value }: { action: string; value?: unknown }) =>
      adminApi.change(account.userId, action, value ?? null),
    onSuccess: (data) => {
      setLink(data.setupUrl ?? '');
      void client.invalidateQueries({ queryKey: ['accounts'] });
    },
  });
  const quota = useMutation({
    mutationFn: () => adminApi.quotas(account.userId, apps, concurrent),
  });
  function action(name: string, value?: unknown) {
    change.mutate({ action: name, value });
  }
  return (
    <details className="card overview-card">
      <summary>
        {account.displayName} · {account.username}
      </summary>
      <p>
        {account.method} · {account.role} · {account.enabled ? account.status : 'disabled'} ·{' '}
        {account.appCount} apps · Last sign-in {time(account.lastSignIn)}
      </p>
      <ErrorNotice error={change.error ?? quota.error} />
      {link && <OneTimeLink value={link} />}
      <div className="field">
        <label htmlFor={`role-${account.userId}`}>Role for {account.username}</label>
        <select
          id={`role-${account.userId}`}
          value={role}
          onChange={(e) => setRole(e.target.value as Account['role'])}
        >
          <option value="owner">Owner</option>
          <option value="staff">Staff</option>
          {account.method === 'local' && <option value="admin">Admin</option>}
        </select>
        <button className="button" onClick={() => action('role', role)} disabled={change.isPending}>
          Change role
        </button>
      </div>
      <div className="topbar-actions">
        <button className="button" onClick={() => action('enabled', !account.enabled)}>
          {account.enabled ? 'Disable account' : 'Enable account'}
        </button>
        <button className="button" onClick={() => action('revoke-sessions')}>
          Revoke sessions
        </button>
        {account.method === 'local' && (
          <>
            <button className="button" onClick={() => action('password-reset')}>
              Password reset link
            </button>
            <button className="button" onClick={() => action('totp-reset')}>
              TOTP reset link
            </button>
            {account.status === 'pending' && (
              <button className="button" onClick={() => action('invite')}>
                New invitation link
              </button>
            )}
          </>
        )}
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          quota.mutate();
        }}
      >
        <h3>Owner quotas</h3>
        <div className="field">
          <label htmlFor={`apps-${account.userId}`}>App limit for {account.username}</label>
          <input
            id={`apps-${account.userId}`}
            type="number"
            min={0}
            max={1000}
            value={apps}
            onChange={(e) => setApps(Number(e.target.value))}
          />
        </div>
        <div className="field">
          <label htmlFor={`concurrent-${account.userId}`}>
            Concurrent operation limit for {account.username}
          </label>
          <input
            id={`concurrent-${account.userId}`}
            type="number"
            min={0}
            max={16}
            value={concurrent}
            onChange={(e) => setConcurrent(Number(e.target.value))}
          />
        </div>
        <button className="button" disabled={quota.isPending}>
          Save quotas
        </button>
        {quota.isSuccess && <p role="status">Quotas saved.</p>}
      </form>
    </details>
  );
}
export function AccountsPage() {
  const [query, setQuery] = useState('');
  const [draft, setDraft] = useState('');
  const [cursor, setCursor] = useState<string>();
  const [username, setUsername] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [role, setRole] = useState('owner');
  const [link, setLink] = useState('');
  const client = useQueryClient();
  const accounts = useQuery({
    queryKey: ['accounts', query, cursor],
    queryFn: () => adminApi.accounts(query, cursor),
    retry: false,
  });
  const create = useMutation({
    mutationFn: () => adminApi.create(username, displayName || username, role),
    onSuccess: (data) => {
      setLink(data.setupUrl);
      setUsername('');
      setDisplayName('');
      void client.invalidateQueries({ queryKey: ['accounts'] });
    },
  });
  function search(e: FormEvent) {
    e.preventDefault();
    setCursor(undefined);
    setQuery(draft);
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">Admin</span>
          <h1>Accounts</h1>
          <p>Manage local and Commons accounts. Privileged roles never change during a session.</p>
        </div>
      </div>
      <Reauthenticate />
      <section className="card overview-card section">
        <h2>Create a local account</h2>
        <ErrorNotice error={create.error} />
        {link && <OneTimeLink value={link} />}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          <div className="field">
            <label htmlFor="create-user">Local username</label>
            <input
              id="create-user"
              maxLength={32}
              required
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="create-display">Display name</label>
            <input
              id="create-display"
              maxLength={256}
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="create-role">Account role</label>
            <select id="create-role" value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="owner">Owner</option>
              <option value="staff">Staff</option>
              <option value="admin">Admin (TOTP required)</option>
            </select>
          </div>
          <button className="button button-primary" disabled={create.isPending}>
            Create invitation
          </button>
        </form>
      </section>
      <form onSubmit={search} className="field">
        <label htmlFor="account-search">Search accounts</label>
        <input
          id="account-search"
          maxLength={64}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
        <button className="button">Search</button>
      </form>
      {accounts.isPending ? (
        <Loading />
      ) : accounts.error ? (
        <ErrorNotice error={accounts.error} />
      ) : (
        accounts.data!.items.map((account) => (
          <AccountControls key={account.userId} account={account} />
        ))
      )}
      <div className="pagination">
        {cursor && (
          <button className="button" onClick={() => setCursor(undefined)}>
            Newest accounts
          </button>
        )}
        {accounts.data?.nextCursor && (
          <button className="button" onClick={() => setCursor(accounts.data!.nextCursor!)}>
            Next page →
          </button>
        )}
      </div>
    </>
  );
}
export function AdminAuditPage() {
  const [cursor, setCursor] = useState<string>();
  const audit = useQuery({
    queryKey: ['admin-audit', cursor],
    queryFn: () => adminApi.audit(cursor),
    retry: false,
  });
  return (
    <>
      <h1>Admin audit</h1>
      {audit.isPending ? (
        <Loading />
      ) : audit.error ? (
        <ErrorNotice error={audit.error} />
      ) : (
        <section className="card overview-card">
          <ul>
            {audit.data!.items.map((row) => (
              <li key={row.id}>
                <strong>{row.action}</strong> · {time(row.createdAt)} · Account{' '}
                {row.targetId ?? 'system'}
                <p className="mono">{JSON.stringify(row.details)}</p>
              </li>
            ))}
          </ul>
        </section>
      )}
      <div className="pagination">
        {cursor && (
          <button className="button" onClick={() => setCursor(undefined)}>
            Newest actions
          </button>
        )}
        {audit.data?.nextCursor && (
          <button className="button" onClick={() => setCursor(audit.data!.nextCursor!)}>
            Next page →
          </button>
        )}
      </div>
    </>
  );
}
