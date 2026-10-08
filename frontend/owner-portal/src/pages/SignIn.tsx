import {
  AuthLayout,
  Button,
  ErrorAlert,
  Field,
  Hint,
  Input,
  PasswordInput,
  buttonClass,
} from '@openstack-platform/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState, type FormEvent } from 'react';
import { useLocation } from 'wouter';
import { clearCredentials } from '../api';
import { authOptionsQuery } from '../authOptions';

const messages: Record<string, string> = {
  INVALID_CREDENTIALS: 'Username, password or authentication code is incorrect.',
  ACCOUNT_DISABLED:
    'This account is disabled or archived. Contact staff if you think this is a mistake.',
  RATE_LIMITED: 'Too many attempts. Wait a minute, then try again.',
  CSRF_REJECTED: 'This page expired. Reload it and try again.',
  AUTH_UNAVAILABLE: 'Sign-in is unavailable right now. Try again in a few minutes.',
  SIGN_IN_UNAVAILABLE: 'Sign-in is unavailable right now. Try again in a few minutes.',
  COMMONS_CANCELLED: 'Sign-in was cancelled.',
  SIGN_IN_EXPIRED: 'That sign-in expired. Try again.',
};

function message(code: unknown, provider: string) {
  if (code === 'IDENTITY_UNAVAILABLE')
    return `Signing in with your ${provider} is unavailable right now. Try again in a minute.`;
  return (
    (typeof code === 'string' && Object.hasOwn(messages, code) && messages[code]) ||
    'Sign-in didn’t work. Try again.'
  );
}

export function SignIn() {
  const [local, setLocal] = useState(false);
  const [totp, setTotp] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [visible, setVisible] = useState(false);
  // Class sign-in comes back here as /sign-in?error=CODE when it doesn't finish.
  const [returned] = useState(() => new URLSearchParams(window.location.search).get('error'));
  const [location, navigate] = useLocation();
  const client = useQueryClient();
  const options = useQuery(authOptionsQuery);
  const provider = options.data?.providerLabel ?? 'class account';
  useEffect(() => {
    // Show the reason once; a reload or a shared link shouldn't repeat it.
    if (window.location.search) navigate(location, { replace: true });
  }, [location, navigate]);
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
            method: 'local',
            totp,
          }),
        });
        const value = await response.json();
        if (!response.ok) throw new Error(message(value.error?.code, provider));
        if (
          typeof value.data?.returnPath !== 'string' ||
          !/^\/(?:apps(?:\/[a-z0-9/-]+)?|all-apps|people|activity|audit)$/.test(
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
  const platformName = options.data?.platformName;
  return (
    <AuthLayout title={platformName ? `Sign in to ${platformName}` : 'Sign in'}>
      <ErrorAlert
        error={
          options.error ??
          login.error ??
          (returned && !login.isPending ? message(returned, provider) : null)
        }
      />
      {/* A plain link: the browser leaves for the class sign-in page and comes back. */}
      <a href="/auth/commons/start" className={buttonClass({ variant: 'primary', block: true })}>
        Sign in with your {provider}
      </a>
      <Hint>You’ll confirm with your {provider} and come right back.</Hint>
      <Button
        variant="ghost"
        block
        aria-expanded={local}
        aria-controls="local-sign-in"
        onClick={() => setLocal(!local)}
      >
        Use a local account
      </Button>
      {local && (
        <form
          id="local-sign-in"
          className="ui-stack ui-gap-4"
          onSubmit={submit}
          aria-busy={login.isPending}
        >
          <Hint>Use the username and password you chose when you set up this account.</Hint>
          <Field label="Username" id="local-username">
            <Input
              name="username"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              maxLength={32}
              required
              autoFocus
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              disabled={login.isPending}
            />
          </Field>
          <Field label="Password" id="local-password">
            <PasswordInput
              name="password"
              autoComplete="current-password"
              maxLength={1024}
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              disabled={login.isPending}
              revealed={visible}
              onRevealedChange={setVisible}
            />
          </Field>
          <Field
            label="Authentication code"
            id="local-totp"
            hint="Only for admin accounts: the 6-digit code from your authenticator app."
          >
            <Input
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              value={totp}
              onChange={(event) => setTotp(event.target.value)}
              disabled={login.isPending}
            />
          </Field>
          <Button type="submit" block loading={login.isPending} disabled={!options.data}>
            Sign in
          </Button>
        </form>
      )}
    </AuthLayout>
  );
}
