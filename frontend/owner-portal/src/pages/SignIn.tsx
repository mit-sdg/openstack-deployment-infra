import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { Link, useLocation, useSearch } from 'wouter';
import { clearCredentials } from '../api';
import { ErrorNotice } from '../components/Feedback';
import { Mark } from '../components/Mark';

const messages: Record<string, string> = {
  INVALID_CREDENTIALS: 'Username or password is incorrect.',
  ACCOUNT_DISABLED: 'Your account is disabled or archived. Contact course staff.',
  IDENTITY_UNAVAILABLE: 'Class sign-in is temporarily unavailable. Please try again.',
  RATE_LIMITED: 'Too many unsuccessful attempts. Wait a minute, then try again.',
  CSRF_REJECTED: 'Reload the sign-in page and try again.',
  STAFF_UNAVAILABLE: 'Staff sign-in is not available for this account.',
};

export function SignIn() {
  const staff = new URLSearchParams(useSearch()).get('mode') === 'staff';
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
            mode: staff ? 'staff' : 'owner',
          }),
        });
        const value = await response.json();
        if (!response.ok)
          throw new Error(
            messages[value.error?.code] ?? 'Sign-in could not complete. Please try again.',
          );
        if (
          typeof value.data?.returnPath !== 'string' ||
          !/^\/(?:apps(?:\/[a-z0-9/-]+)?|activity|staff\/owners)$/.test(value.data.returnPath)
        )
          throw new Error('Invalid sign-in response.');
        return value.data.returnPath as string;
      } finally {
        setPassword('');
        setVisible(false);
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
        <span className="eyebrow">
          {staff ? 'Staff view · Read only' : 'Your application workspace'}
        </span>
        <h1>
          {staff ? 'Course applications.' : 'Make something.'}
          <br />
          {staff ? 'See what’s running.' : 'Put it out there.'}
        </h1>
        <p>
          {staff
            ? 'Review owners, quotas, deployment history, and application status.'
            : 'Create an app, connect your repository, and turn a specific commit into a running application.'}
        </p>
        <div className="intro-steps">
          <span>
            <b>01</b> {staff ? 'Review owners' : 'Connect your code'}
          </span>
          <span>
            <b>02</b> {staff ? 'Browse applications' : 'Choose your settings'}
          </span>
          <span>
            <b>03</b> {staff ? 'Check operations' : 'Deploy with confidence'}
          </span>
        </div>
      </div>
      <section className="card sign-in-card">
        <span className="large-brand">
          <Mark />
        </span>
        <h2>
          {staff ? 'Staff sign-in with your ' : 'Sign in with your '}
          {options.data?.providerLabel ?? 'class account'}
        </h2>
        <p>Use the same username and password as your class account.</p>
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
                maxLength={128}
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
          <button
            className="button button-primary sign-in-button"
            disabled={!options.data || login.isPending}
          >
            {login.isPending ? 'Checking your account…' : 'Sign in'}
          </button>
        </form>
        <small>
          Your password is checked server-side with your{' '}
          {options.data?.providerLabel ?? 'class account'} and is never saved by the portal. Portal
          {staff
            ? 'staff sessions last up to 1 hour, or 10 minutes without activity.'
            : 'sessions last up to 8 hours, or 30 minutes without activity.'}
        </small>
        <p>
          <Link href={staff ? '/sign-in' : '/signin?mode=staff'} className="text-link">
            {staff ? 'Owner sign-in' : 'Staff sign-in'}
          </Link>
        </p>
      </section>
    </div>
  );
}
