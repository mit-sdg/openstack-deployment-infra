import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState, type FormEvent } from 'react';
import { Link, useLocation } from 'wouter';
import { clearCredentials } from '../api';
import { ErrorNotice, Loading } from '../components/Feedback';

async function post(path: string, body: unknown) {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok)
    throw new Error(
      typeof payload.error?.summary === 'string'
        ? payload.error.summary
        : 'Enrollment could not complete.',
    );
  return payload.data;
}
export function Enrollment() {
  const [token] = useState(() => window.location.hash.slice(1));
  const [username, setUsername] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [password, setPassword] = useState('');
  const [totp, setTotp] = useState('');
  const [enabled, setEnabled] = useState(false);
  const [stage, setStage] = useState<{
    enrollmentToken: string;
    totpSecret: string | null;
    otpauthUri: string | null;
  }>();
  const [, navigate] = useLocation();
  const client = useQueryClient();
  useEffect(() => {
    window.history.replaceState(null, '', window.location.pathname);
  }, []);
  const options = useQuery({
    queryKey: ['enrollment-options'],
    queryFn: async () => {
      const response = await fetch('/auth/options', {
        credentials: 'same-origin',
        cache: 'no-store',
      });
      const data = (await response.json()).data;
      if (!response.ok || typeof data?.csrfToken !== 'string')
        throw new Error('Enrollment is temporarily unavailable.');
      return data as { csrfToken: string };
    },
    retry: false,
  });
  const info = useQuery({
    queryKey: ['enrollment-info'],
    enabled: !!options.data && !!token,
    queryFn: () => post('/auth/token-info', { token, csrfToken: options.data!.csrfToken }),
    retry: false,
  });
  const begin = useMutation({
    mutationFn: async () => {
      if (info.data.purpose !== 'totp-reset' && new TextEncoder().encode(password).length > 1024)
        throw new Error('Password exceeds 1024 bytes.');
      return post('/auth/enroll', {
        token,
        csrfToken: options.data!.csrfToken,
        password,
        totp,
        username: info.data.username ?? username,
        displayName: displayName || username,
        totpEnabled: info.data.role === 'admin' || enabled,
      });
    },
    onSuccess: (value) => {
      setStage(value);
      setPassword('');
      setTotp('');
    },
  });
  const finish = useMutation({
    mutationFn: () =>
      post('/auth/enroll/finish', {
        csrfToken: options.data!.csrfToken,
        enrollmentToken: stage!.enrollmentToken,
        totp,
      }),
    onSuccess: (data) => {
      if (!['/apps', '/admin/accounts', '/sign-in'].includes(data.returnPath))
        throw new Error('Invalid enrollment result');
      setStage(undefined);
      setTotp('');
      clearCredentials();
      client.clear();
      navigate(data.returnPath);
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    if (stage) finish.mutate();
    else begin.mutate();
  }
  if (!token && !stage)
    return <ErrorNotice error={new Error('Open the complete setup or invitation link.')} />;
  if (options.isPending || info.isPending) return <Loading />;
  if (options.error || info.error) return <ErrorNotice error={options.error ?? info.error} />;
  const resetTotp = info.data?.purpose === 'totp-reset';
  return (
    <section className="card overview-card">
      <span className="eyebrow">Local portal account</span>
      <h1>
        {info.data?.purpose === 'bootstrap'
          ? 'Set up an admin account'
          : resetTotp
            ? 'Reset authentication codes'
            : info.data?.purpose === 'password-reset'
              ? 'Reset your password'
              : 'Accept your invitation'}
      </h1>
      <ErrorNotice error={begin.error ?? finish.error} />
      <form onSubmit={submit}>
        {!stage ? (
          <>
            {info.data.username ? (
              <p>
                Account: <strong>{info.data.username}</strong>
              </p>
            ) : (
              <>
                <div className="field">
                  <label htmlFor="enroll-name">Local username</label>
                  <input
                    id="enroll-name"
                    autoComplete="username"
                    maxLength={32}
                    required
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                  />
                </div>
                <div className="field">
                  <label htmlFor="enroll-display">Display name</label>
                  <input
                    id="enroll-display"
                    maxLength={256}
                    value={displayName}
                    onChange={(e) => setDisplayName(e.target.value)}
                  />
                </div>
              </>
            )}
            {!resetTotp && (
              <div className="field">
                <label htmlFor="enroll-password">New password</label>
                <input
                  id="enroll-password"
                  type="password"
                  autoComplete="new-password"
                  minLength={12}
                  maxLength={1024}
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
                <p className="field-help">
                  At least 12 characters, at most 1024 UTF-8 bytes. Do not include the username.
                </p>
              </div>
            )}
            {info.data.purpose === 'password-reset' && info.data.totpEnabled && (
              <div className="field">
                <label htmlFor="existing-code">Current authentication code</label>
                <input
                  id="existing-code"
                  inputMode="numeric"
                  maxLength={6}
                  required
                  value={totp}
                  onChange={(e) => setTotp(e.target.value)}
                />
              </div>
            )}
            {info.data.purpose === 'invite' && info.data.role !== 'admin' && (
              <label>
                <input
                  type="checkbox"
                  checked={enabled}
                  onChange={(e) => setEnabled(e.target.checked)}
                />{' '}
                Enable authentication codes (TOTP)
              </label>
            )}
            {info.data.role === 'admin' && (
              <p>Admin accounts require authentication-code enrollment before they can sign in.</p>
            )}
            <button className="button button-primary" disabled={begin.isPending}>
              {begin.isPending ? 'Preparing…' : 'Continue'}
            </button>
          </>
        ) : (
          <>
            {stage.totpSecret ? (
              <>
                <h2>Enroll authentication codes</h2>
                <p>
                  Add this key to your authenticator. It is shown only once; save it before
                  continuing.
                </p>
                <code className="mono" data-testid="totp-secret">
                  {stage.totpSecret}
                </code>
                <p>
                  {stage.otpauthUri && <a href={stage.otpauthUri}>Open in your authenticator</a>}
                </p>
                <div className="field">
                  <label htmlFor="new-code">Authentication code</label>
                  <input
                    id="new-code"
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    maxLength={6}
                    required
                    value={totp}
                    onChange={(e) => setTotp(e.target.value)}
                  />
                </div>
              </>
            ) : (
              <p>Your account is ready to activate.</p>
            )}
            <button className="button button-primary" disabled={finish.isPending}>
              {finish.isPending ? 'Verifying…' : 'Finish enrollment'}
            </button>
          </>
        )}
      </form>
      <p>
        <Link href="/sign-in">Return to sign-in</Link>
      </p>
    </section>
  );
}
