import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useLocation } from 'wouter';
import { clearCredentials } from '../api';
import { ErrorNotice } from '../components/Feedback';
import { Mark } from '../components/Mark';

const messages: Record<string, string> = {
  INVALID_CREDENTIALS: 'Username or password is incorrect.',
  ACCOUNT_DISABLED: 'Your account is disabled or archived. Contact course staff.',
  IDENTITY_UNAVAILABLE: 'Class sign-in is temporarily unavailable. Please try again.',
  RATE_LIMITED: 'Too many unsuccessful attempts. Wait a minute, then try again.',
  CSRF_REJECTED: 'Reload the sign-in page and try again.',
  AUTH_UNAVAILABLE: 'Local sign-in is temporarily unavailable.',
};

export function SignIn() {
  const [method, setMethod] = useState<'commons' | 'local'>('commons');
  const [totp, setTotp] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [visible, setVisible] = useState(false);
  const [, navigate] = useLocation();
  const client = useQueryClient();
  const options = useQuery({
    queryKey: ['auth-options'],
    queryFn: async () => {
      const response = await fetch('/auth/options', {
        credentials: 'same-origin',
        cache: 'no-store',
      });
      if (!response.ok) throw new Error('Sign-in is temporarily unavailable.');
      const value = await response.json();
      if (
        typeof value.data?.csrfToken !== 'string' ||
        typeof value.data?.providerLabel !== 'string'
      )
        throw new Error('Invalid sign-in response.');
      return value.data as { csrfToken: string; providerLabel: string };
    },
  });
  const login = useMutation({
    mutationFn: async () => {
      try {
        const response = await fetch('/auth/login', {
          method: 'POST',
          credentials: 'same-origin',
          cache: 'no-store',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            csrfToken: options.data?.csrfToken,
            username,
            password,
            method,
            totp,
          }),
        });
        const value = await response.json();
        if (!response.ok)
          throw new Error(
            (method === 'local' && value.error?.code === 'INVALID_CREDENTIALS'
              ? 'Username, password or authentication code is incorrect.'
              : messages[value.error?.code]) ?? 'Sign-in could not complete. Please try again.',
          );
        if (
          typeof value.data?.returnPath !== 'string' ||
          !/^\/(?:apps(?:\/[a-z0-9/-]+)?|activity|staff\/owners|admin\/accounts)$/.test(
            value.data.returnPath,
          )
        )
          throw new Error('Invalid sign-in response.');
        return value.data.returnPath as string;
      } finally {
        setPassword('');
        setVisible(false);
        setTotp('');
      }
    },
    onSuccess: (path) => {
      clearCredentials();
      client.clear();
      navigate(path);
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    login.mutate();
  }
  return (
    <div className="sign-in-layout">
      <div className="sign-in-intro">
        <span className="eyebrow">Your application workspace</span>
        <h1>
          Make something.
          <br />
          Put it out there.
        </h1>
        <p>Create an app, connect your repository, and deploy a specific commit.</p>
        <div className="intro-steps">
          <span>
            <b>01</b> Connect your code
          </span>
          <span>
            <b>02</b> Choose your settings
          </span>
          <span>
            <b>03</b> Deploy with confidence
          </span>
        </div>
      </div>
      <section className="card sign-in-card">
        <span className="large-brand">
          <Mark />
        </span>
        <h2>
          Sign in with your{' '}
          {method === 'local'
            ? 'local portal account'
            : (options.data?.providerLabel ?? 'class account')}
        </h2>
        <p>
          {method === 'commons'
            ? 'Use the same username and password as your class account.'
            : 'Use the local portal credentials you enrolled.'}
        </p>
        <fieldset className="field">
          <legend>Sign-in method</legend>
          <label>
            <input
              type="radio"
              name="method"
              checked={method === 'commons'}
              onChange={() => setMethod('commons')}
            />{' '}
            Commons
          </label>
          <label>
            <input
              type="radio"
              name="method"
              checked={method === 'local'}
              onChange={() => setMethod('local')}
            />{' '}
            Local portal account
          </label>
        </fieldset>
        <ErrorNotice error={options.error ?? login.error} />
        <form onSubmit={submit} aria-busy={login.isPending}>
          <div className="field">
            <label htmlFor="class-username">Username</label>
            <input
              id="class-username"
              name="username"
              autoComplete="username"
              maxLength={32}
              required
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              disabled={login.isPending}
            />
          </div>
          <div className="field">
            <label htmlFor="class-password">Password</label>
            <div className="password-control">
              <input
                id="class-password"
                name="password"
                type={visible ? 'text' : 'password'}
                autoComplete="current-password"
                maxLength={method === 'local' ? 1024 : 128}
                required
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                disabled={login.isPending}
              />
              <button
                className="password-toggle"
                type="button"
                aria-label={visible ? 'Hide password' : 'Show password'}
                aria-pressed={visible}
                onClick={() => setVisible(!visible)}
                disabled={login.isPending}
              >
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z" />
                  <circle cx="12" cy="12" r="3" />
                  {visible && <path d="m3 3 18 18" />}
                </svg>
              </button>
            </div>
          </div>
          {method === 'local' && (
            <div className="field">
              <label htmlFor="local-totp">Authentication code (when enabled)</label>
              <input
                id="local-totp"
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={6}
                value={totp}
                onChange={(event) => setTotp(event.target.value)}
              />
            </div>
          )}
          <button
            className="button button-primary sign-in-button"
            disabled={!options.data || login.isPending}
          >
            {login.isPending ? 'Checking your account…' : 'Sign in'}
          </button>
        </form>
        <small>
          {method === 'commons'
            ? 'Commons passwords are checked server-side and never saved by the portal.'
            : 'Local passwords are stored as salted password hashes. Admin accounts require an authentication code.'}
        </small>
      </section>
    </div>
  );
}
