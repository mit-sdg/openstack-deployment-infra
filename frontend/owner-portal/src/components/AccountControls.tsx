import {
  Alert,
  Button,
  CopyField,
  Dialog,
  ErrorAlert,
  Field,
  Grid,
  Input,
  KeyValueList,
  RelativeTime,
  Section,
  SectionSkeleton,
  Select,
  Stack,
  useToast,
} from '@openstack-platform/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent, type ReactNode } from 'react';
import { adminApi, type Account } from '../adminApi';
import { QueryError } from '../components/Feedback';
import { friendly, useProviderLabel, useStepUp } from '../pages/admin/common';

const linkHint = 'Send it privately. It works once and expires in 72 hours.';

function When({ value }: { value: string | null }) {
  return (
    <span className="ui-text-muted">
      <RelativeTime value={value} empty="Never" />
    </span>
  );
}

export function CreateAccountAction() {
  const [creating, setCreating] = useState(false);
  const stepUp = useStepUp();
  return (
    <>
      <Button variant="primary" icon="plus" onClick={() => setCreating(true)}>
        Create account
      </Button>
      <CreateAccountDialog open={creating} onClose={() => setCreating(false)} run={stepUp.run} />
      {stepUp.dialog}
    </>
  );
}
export function AccountControls({ id }: { id: string }) {
  const stepUp = useStepUp();
  const provider = useProviderLabel();
  const account = useQuery({ queryKey: ['account', id], queryFn: () => adminApi.account(id) });
  return (
    <>
      <Section title="Account controls" aria-label="Account controls">
        {account.isPending ? (
          <SectionSkeleton rows={3} />
        ) : account.error ? (
          <QueryError query={account} what="this account" />
        ) : (
          <ManageAccount
            key={account.data.role + String(account.data.appLimit)}
            account={account.data}
            provider={provider}
            run={stepUp.run}
          />
        )}
      </Section>
      {stepUp.dialog}
    </>
  );
}

function CreateAccountDialog({
  open,
  onClose,
  run,
}: {
  open: boolean;
  onClose: () => void;
  run: ReturnType<typeof useStepUp>['run'];
}) {
  const [username, setUsername] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [role, setRole] = useState<Account['role']>('owner');
  const [link, setLink] = useState('');
  const client = useQueryClient();
  const create = useMutation({
    mutationFn: () => run(() => adminApi.create(username, displayName || username, role)),
    onSuccess: (data) => {
      setLink(data.setupUrl);
      void client.invalidateQueries({ queryKey: ['class'] });
    },
  });
  function close() {
    onClose();
    if (link) {
      setLink('');
      setUsername('');
      setDisplayName('');
      setRole('owner');
      create.reset();
    }
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate();
  }
  return (
    <Dialog
      open={open}
      onClose={close}
      title={link ? 'Account created' : 'Create account'}
      footer={
        link ? (
          <Button variant="primary" onClick={close}>
            Done
          </Button>
        ) : (
          <>
            <Button onClick={close}>Cancel</Button>
            <Button
              type="submit"
              form="create-account"
              variant="primary"
              loading={create.isPending}
            >
              Create account
            </Button>
          </>
        )
      }
    >
      {link ? (
        <>
          <p>
            {displayName || username} sets a password with this link
            {role === 'admin' ? ' and adds an authenticator app' : ''}.
          </p>
          <CopyField label="Setup link" value={link} hint={linkHint} />
        </>
      ) : (
        <form id="create-account" className="ui-stack ui-gap-4" onSubmit={submit}>
          <ErrorAlert error={friendly(create.error)} />
          <Field label="Username" id="create-user">
            <Input
              maxLength={32}
              required
              autoCapitalize="none"
              autoComplete="off"
              spellCheck={false}
              value={username}
              onChange={(event) => setUsername(event.target.value)}
            />
          </Field>
          <Field label="Display name" id="create-display" optional>
            <Input
              maxLength={256}
              autoComplete="off"
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
            />
          </Field>
          <Field
            label="Role"
            id="create-role"
            hint={
              role === 'admin'
                ? 'Admins sign in with a password and an authenticator app.'
                : undefined
            }
          >
            <Select
              value={role}
              onChange={(event) => setRole(event.target.value as Account['role'])}
            >
              <option value="owner">Owner</option>
              <option value="staff">Staff</option>
              <option value="admin">Admin</option>
            </Select>
          </Field>
        </form>
      )}
    </Dialog>
  );
}

function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Stack gap={3}>
      <h3 className="admin-group-title">{title}</h3>
      {children}
    </Stack>
  );
}

const linkActions: Record<string, string> = {
  'password-reset': 'Password reset link created',
  'totp-reset': 'Authenticator reset link created',
  invite: 'New invitation link created',
};

function ManageAccount({
  account,
  provider,
  run,
}: {
  account: Account;
  provider: string;
  run: ReturnType<typeof useStepUp>['run'];
}) {
  const [role, setRole] = useState(account.role);
  const [apps, setApps] = useState(account.appLimit ?? 0);
  const [concurrent, setConcurrent] = useState(account.concurrencyLimit ?? 0);
  const [link, setLink] = useState<{ title: string; url: string } | null>(null);
  const client = useQueryClient();
  const toast = useToast();
  const [disable, setDisable] = useState(false);
  const local = account.method === 'local';
  const change = useMutation({
    mutationFn: ({ action, value }: { action: string; value?: unknown }) =>
      run(() => adminApi.change(account.userId, action, value ?? null)),
    onSuccess: (data, { action, value }) => {
      setLink(
        data.setupUrl
          ? { title: linkActions[action] ?? 'Setup link created', url: data.setupUrl }
          : null,
      );
      if (!data.setupUrl)
        toast(
          action === 'role'
            ? 'Role changed'
            : action === 'enabled'
              ? value
                ? 'Account enabled'
                : 'Account disabled'
              : 'Signed out everywhere',
        );
      void client.invalidateQueries({ queryKey: ['account', account.userId] });
      void client.invalidateQueries({ queryKey: ['class'] });
    },
  });
  const quota = useMutation({
    mutationFn: () => adminApi.quotas(account.userId, apps, concurrent),
    onSuccess: () => {
      toast('Limits saved');
      void client.invalidateQueries({ queryKey: ['account', account.userId] });
      void client.invalidateQueries({ queryKey: ['class'] });
    },
  });
  const busy = change.isPending ? change.variables?.action : null;
  function action(name: string, value?: unknown) {
    change.mutate({ action: name, value });
  }
  return (
    <Stack gap={6}>
      <KeyValueList
        columns={2}
        items={[
          {
            label: 'Sign-in',
            value: local
              ? account.totpEnabled
                ? 'Local, with authenticator app'
                : 'Local'
              : provider,
          },
          { label: 'Last sign-in', value: <When value={account.lastSignIn} /> },
        ]}
      />
      <ErrorAlert error={friendly(change.error) ?? quota.error} />
      {link && (
        <Alert tone="success" title={link.title}>
          <CopyField label="Setup link" value={link.url} hint={linkHint} />
        </Alert>
      )}
      <Stack gap={6}>
        <form
          className="ui-cluster ui-gap-2 admin-inline-form"
          onSubmit={(event) => {
            event.preventDefault();
            action('role', role);
          }}
        >
          <Field label="Role" id={`role-${account.userId}`}>
            <Select
              value={role}
              onChange={(event) => setRole(event.target.value as Account['role'])}
            >
              <option value="owner">Owner</option>
              <option value="staff">Staff</option>
              {local && <option value="admin">Admin</option>}
            </Select>
          </Field>
          <Button type="submit" disabled={role === account.role} loading={busy === 'role'}>
            Change role
          </Button>
        </form>
        {/* Staff and admin accounts have no limits. */}
        {account.appLimit !== null && (
          <Group title="Limits">
            <form
              className="ui-stack ui-gap-4"
              onSubmit={(event) => {
                event.preventDefault();
                quota.mutate();
              }}
            >
              <Grid columns={2}>
                <Field label="Apps" id={`apps-${account.userId}`}>
                  <Input
                    type="number"
                    min={0}
                    max={1000}
                    required
                    value={apps}
                    onChange={(event) => setApps(Number(event.target.value))}
                  />
                </Field>
                <Field label="Changes at a time" id={`concurrent-${account.userId}`}>
                  <Input
                    type="number"
                    min={0}
                    max={16}
                    required
                    value={concurrent}
                    onChange={(event) => setConcurrent(Number(event.target.value))}
                  />
                </Field>
              </Grid>
              <div>
                <Button type="submit" loading={quota.isPending}>
                  Save limits
                </Button>
              </div>
            </form>
          </Group>
        )}
        <Group title="Access">
          <div className="ui-cluster ui-gap-2">
            {local && account.status === 'pending' && (
              <Button loading={busy === 'invite'} onClick={() => action('invite')}>
                New invitation link
              </Button>
            )}
            {local && (
              <>
                <Button
                  loading={busy === 'password-reset'}
                  onClick={() => action('password-reset')}
                >
                  Reset password
                </Button>
                <Button loading={busy === 'totp-reset'} onClick={() => action('totp-reset')}>
                  Reset authenticator
                </Button>
              </>
            )}
            <Button loading={busy === 'revoke-sessions'} onClick={() => action('revoke-sessions')}>
              Sign out everywhere
            </Button>
            <Button
              variant={account.enabled ? 'danger' : 'secondary'}
              loading={busy === 'enabled'}
              onClick={() => (account.enabled ? setDisable(true) : action('enabled', true))}
            >
              {account.enabled ? 'Disable account' : 'Enable account'}
            </Button>
          </div>
        </Group>
      </Stack>
      <Dialog
        open={disable}
        onClose={() => setDisable(false)}
        title={`Disable ${account.displayName}?`}
        footer={
          <>
            <Button onClick={() => setDisable(false)}>Cancel</Button>
            <Button
              variant="danger"
              loading={busy === 'enabled'}
              onClick={() => {
                action('enabled', false);
                setDisable(false);
              }}
            >
              Disable account
            </Button>
          </>
        }
      >
        <p>This person will be signed out and cannot sign in. Their apps are kept.</p>
      </Dialog>
    </Stack>
  );
}
