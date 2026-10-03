import {
  Alert,
  AuthLayout,
  Button,
  Checkbox,
  ErrorAlert,
  Field,
  Hint,
  Icon,
  Input,
  LoadingRows,
  PasswordInput,
  buttonClass,
} from '@openstack-platform/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Link, useLocation } from 'wouter';
import { clearCredentials } from '../api';
import { authOptionsQuery } from '../authOptions';
import { Mark } from '../components/Mark';
import './admin/admin.css';

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
        : 'Setup couldn’t finish. Reload the page and try again.',
    );
  return payload.data;
}

const titles: Record<string, string> = {
  bootstrap: 'Set up your admin account',
  'password-reset': 'Reset your password',
  'totp-reset': 'Set up your authenticator',
  invite: 'Set up your account',
};

/** Groups of four characters; copying the text gives the key without spaces. */
function Secret({ value }: { value: string }) {
  return (
    <code className="admin-secret" data-testid="totp-secret">
      {value.match(/.{1,4}/g)?.map((group, index) => (
        <span key={index}>{group}</span>
      ))}
    </code>
  );
}

function CopySecret({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  return (
    <Button
      size="sm"
      icon={copied ? 'check' : 'copy'}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setCopied(true);
          window.clearTimeout(timer.current);
          timer.current = window.setTimeout(() => setCopied(false), 2000);
        } catch {
          // Clipboard blocked: the key stays visible to type in.
        }
      }}
    >
      {copied ? 'Copied' : 'Copy key'}
    </Button>
  );
}

export function Enrollment() {
  const [token] = useState(() => window.location.hash.slice(1));
  const [username, setUsername] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [password, setPassword] = useState('');
  const [visible, setVisible] = useState(false);
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
  // Shared with the shell, which shows the platform name from it.
  const options = useQuery({ ...authOptionsQuery, retry: false });
  const info = useQuery({
    queryKey: ['enrollment-info'],
    enabled: !!options.data && !!token,
    queryFn: () => post('/auth/token-info', { token, csrfToken: options.data!.csrfToken }),
    retry: false,
  });
  const begin = useMutation({
    mutationFn: async () => {
      if (info.data.purpose !== 'totp-reset' && new TextEncoder().encode(password).length > 1024)
        throw new Error('Use a shorter password: at most 1024 bytes.');
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
      setVisible(false);
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
  const footer = (
    <Link href="/sign-in" className="ui-link">
      Back to sign in
    </Link>
  );
  if (!token && !stage)
    return (
      <AuthLayout title="Set up your account" footer={footer} mark={<Mark />}>
        <Alert tone="danger" title="This link is incomplete">
          Open the full setup link you were sent, or ask an admin for a new one.
        </Alert>
      </AuthLayout>
    );
  if (options.isPending || info.isPending)
    return (
      <AuthLayout title="Set up your account" footer={footer} mark={<Mark />}>
        <LoadingRows rows={3} />
      </AuthLayout>
    );
  if (options.error || info.error)
    return (
      <AuthLayout title="Set up your account" footer={footer} mark={<Mark />}>
        <ErrorAlert error={options.error ?? info.error} />
      </AuthLayout>
    );
  const purpose: string = info.data?.purpose ?? 'invite';
  const resetTotp = purpose === 'totp-reset';
  const admin = info.data.role === 'admin';
  if (stage)
    return (
      <AuthLayout
        title={stage.totpSecret ? 'Add your authenticator' : 'Finish setup'}
        footer={footer}
        mark={<Mark />}
      >
        <ErrorAlert error={finish.error} />
        <form className="ui-stack ui-gap-4" onSubmit={submit} aria-busy={finish.isPending}>
          {stage.totpSecret ? (
            <>
              <Hint>
                Add this key to an authenticator app, then enter the 6-digit code it shows. The key
                is shown only once.
              </Hint>
              <Secret value={stage.totpSecret} />
              <div className="ui-cluster ui-gap-2">
                <CopySecret value={stage.totpSecret} />
                {stage.otpauthUri && (
                  <a href={stage.otpauthUri} className={buttonClass({ size: 'sm' })}>
                    <Icon name="external" />
                    Open in authenticator
                  </a>
                )}
              </div>
              <Field label="Authentication code" id="new-code">
                <Input
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  maxLength={6}
                  required
                  value={totp}
                  onChange={(event) => setTotp(event.target.value)}
                  disabled={finish.isPending}
                />
              </Field>
            </>
          ) : (
            <p>Your account is ready.</p>
          )}
          <Button type="submit" variant="primary" block loading={finish.isPending}>
            Finish setup
          </Button>
        </form>
      </AuthLayout>
    );
  return (
    <AuthLayout title={titles[purpose] ?? titles.invite} footer={footer} mark={<Mark />}>
      <ErrorAlert error={begin.error} />
      <form className="ui-stack ui-gap-4" onSubmit={submit} aria-busy={begin.isPending}>
        {info.data.username ? (
          <Field label="Username" id="enroll-name">
            <Input autoComplete="username" readOnly value={info.data.username} />
          </Field>
        ) : (
          <>
            <Field label="Username" id="enroll-name">
              <Input
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
                maxLength={32}
                required
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                disabled={begin.isPending}
              />
            </Field>
            <Field label="Display name" id="enroll-display" optional>
              <Input
                autoComplete="name"
                maxLength={256}
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
                disabled={begin.isPending}
              />
            </Field>
          </>
        )}
        {!resetTotp && (
          <Field
            label="New password"
            id="enroll-password"
            hint="At least 12 characters. Don’t include your username."
          >
            <PasswordInput
              autoComplete="new-password"
              minLength={12}
              maxLength={1024}
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              disabled={begin.isPending}
              revealed={visible}
              onRevealedChange={setVisible}
            />
          </Field>
        )}
        {purpose === 'password-reset' && info.data.totpEnabled && (
          <Field
            label="Current authentication code"
            id="existing-code"
            hint="The 6-digit code from your authenticator app."
          >
            <Input
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              required
              value={totp}
              onChange={(event) => setTotp(event.target.value)}
              disabled={begin.isPending}
            />
          </Field>
        )}
        {purpose === 'invite' && !admin && (
          <Checkbox
            label="Use an authenticator app"
            description="Ask for a 6-digit code each time you sign in."
            checked={enabled}
            onChange={(event) => setEnabled(event.target.checked)}
            disabled={begin.isPending}
          />
        )}
        {admin && !resetTotp && (
          <Hint>Admin accounts need an authenticator app. You’ll add it on the next step.</Hint>
        )}
        <Button type="submit" variant="primary" block loading={begin.isPending}>
          Continue
        </Button>
      </form>
    </AuthLayout>
  );
}
