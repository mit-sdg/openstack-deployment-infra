import {
  AuthLayout,
  Button,
  ErrorAlert,
  Field,
  Input,
  PasswordInput,
  SegmentedControl,
} from '@openstack-platform/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useLocation } from 'wouter';
import { clearCredentials } from '../api';
import { authOptionsQuery } from '../authOptions';

const messages: Record<string, string> = {
  INVALID_CREDENTIALS: 'Username or password is incorrect.',
  ACCOUNT_DISABLED:
    'This account is disabled or archived. Contact staff if you think this is a mistake.',
  RATE_LIMITED: 'Too many attempts. Wait a minute, then try again.',
  CSRF_REJECTED: 'This page expired. Reload it and try again.',
  AUTH_UNAVAILABLE: 'Sign-in is unavailable right now. Try again in a few minutes.',
};

function sentenceCase(value: string) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

export function SignIn() {
  const [method, setMethod] = useState<'commons' | 'local'>('commons');
  const [totp, setTotp] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [visible, setVisible] = useState(false);
  const [, navigate] = useLocation();
  const client = useQueryClient();
  const options = useQuery(authOptionsQuery);
  const provider = options.data?.providerLabel ?? 'class account';
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
              : value.error?.code === 'IDENTITY_UNAVAILABLE'
                ? `Signing in with your ${provider} is unavailable right now. Try again in a few minutes.`
                : messages[value.error?.code]) ?? 'Sign-in didn’t work. Try again.',
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
  const platformName = options.data?.platformName;
  return (
    <AuthLayout title={platformName ? `Sign in to ${platformName}` : 'Sign in'}>
      <SegmentedControl
        label="Sign-in method"
        hideLabel
        name="method"
        block
        value={method}
        onChange={setMethod}
        options={[
          { value: 'commons', label: sentenceCase(provider) },
          { value: 'local', label: 'Local account' },
        ]}
        hint={
          method === 'local'
            ? 'Use the username and password you chose when you set up this account.'
            : undefined
        }
      />
      <ErrorAlert error={options.error ?? login.error} />
      <form className="ui-stack ui-gap-4" onSubmit={submit} aria-busy={login.isPending}>
        <Field label="Username" id="class-username">
          <Input
            name="username"
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            maxLength={32}
            required
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            disabled={login.isPending}
          />
        </Field>
        <Field label="Password" id="class-password">
          <PasswordInput
            name="password"
            autoComplete="current-password"
            maxLength={method === 'local' ? 1024 : 128}
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            disabled={login.isPending}
            revealed={visible}
            onRevealedChange={setVisible}
          />
        </Field>
        {method === 'local' && (
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
        )}
        <Button
          type="submit"
          variant="primary"
          block
          loading={login.isPending}
          disabled={!options.data}
        >
          Sign in
        </Button>
      </form>
    </AuthLayout>
  );
}
