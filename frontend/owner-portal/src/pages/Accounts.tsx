import {
  Alert,
  Badge,
  Button,
  CopyField,
  DataTable,
  Dialog,
  EmptyState,
  ErrorAlert,
  Field,
  Grid,
  Input,
  KeyValueList,
  Page,
  PageHeader,
  PageSkeleton,
  Section,
  Select,
  Stack,
  useToast,
  type Column,
  type Tone,
} from '@openstack-platform/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent, type ReactNode } from 'react';
import { adminApi, type Account, type AdminAudit } from '../adminApi';
import { relativeTime } from '../utils/presentation';
import {
  AccountName,
  CopyId,
  friendly,
  roleNames,
  useAccountNames,
  useDebounced,
  useProviderLabel,
  useStepUp,
} from './admin/common';

const linkHint = 'Send it privately. It works once and expires in 72 hours.';

function When({ value }: { value: string | null }) {
  return value ? (
    <time className="ui-text-muted" dateTime={value} title={new Date(value).toLocaleString()}>
      {relativeTime(value)}
    </time>
  ) : (
    <span className="ui-text-subtle">Never</span>
  );
}

function accountState(account: Account): { label: string; tone: Tone } {
  if (!account.enabled) return { label: 'Disabled', tone: 'neutral' };
  if (account.status === 'pending') return { label: 'Setup pending', tone: 'warning' };
  return { label: 'Active', tone: 'success' };
}

function RoleBadge({ role }: { role: Account['role'] }) {
  return (
    <Badge tone={role === 'owner' ? 'neutral' : 'info'} dot={false}>
      {roleNames[role]}
    </Badge>
  );
}

export function AccountsPage() {
  const [search, setSearch] = useState('');
  const query = useDebounced(search.trim());
  const [cursor, setCursor] = useState<string>();
  const [creating, setCreating] = useState(false);
  const [managing, setManaging] = useState<Account | null>(null);
  const provider = useProviderLabel();
  const stepUp = useStepUp();
  const accounts = useQuery({
    queryKey: ['accounts', query, cursor],
    queryFn: () => adminApi.accounts(query, cursor),
    retry: false,
    placeholderData: (previous) => previous,
  });
  const columns: Column<Account>[] = [
    {
      key: 'name',
      header: 'Name',
      mobile: 'title',
      cell: (account) => (
        <span className="ui-stack ui-gap-1">
          <span>{account.displayName}</span>
          <span className="ui-text-muted ui-text-sm">{account.username}</span>
        </span>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      mobile: 'trailing',
      cell: (account) => {
        const state = accountState(account);
        return <Badge tone={state.tone}>{state.label}</Badge>;
      },
    },
    { key: 'role', header: 'Role', cell: (account) => <RoleBadge role={account.role} /> },
    {
      key: 'method',
      header: 'Sign-in',
      cell: (account) => (
        <span className="ui-text-muted">{account.method === 'local' ? 'Local' : provider}</span>
      ),
    },
    {
      key: 'apps',
      header: 'Apps',
      cell: (account) => (
        <span className="ui-text-muted">
          {account.appCount} of {account.appLimit}
        </span>
      ),
    },
    {
      key: 'last',
      header: 'Last sign-in',
      cell: (account) => <When value={account.lastSignIn} />,
    },
    {
      key: 'actions',
      header: 'Actions',
      hideHeader: true,
      align: 'end',
      cell: (account) => (
        <Button
          size="sm"
          variant="ghost"
          aria-label={`Manage ${account.username}`}
          onClick={() => setManaging(account)}
        >
          Manage
        </Button>
      ),
    },
  ];
  if (accounts.isPending && !accounts.data) return <PageSkeleton />;
  const items = accounts.data?.items ?? [];
  // Keep the dialog on fresh data after a change.
  const current = managing && (items.find((a) => a.userId === managing.userId) ?? managing);
  return (
    <Page>
      <PageHeader
        title="Accounts"
        actions={
          <Button variant="primary" icon="plus" onClick={() => setCreating(true)}>
            Create account
          </Button>
        }
      />
      <Grid columns={3}>
        <Input
          type="search"
          aria-label="Search accounts"
          placeholder="Search by name or username"
          maxLength={64}
          autoComplete="off"
          value={search}
          onChange={(event) => {
            setSearch(event.target.value);
            setCursor(undefined);
          }}
        />
      </Grid>
      <ErrorAlert error={accounts.error} />
      {accounts.data &&
        (items.length ? (
          <Section
            flush
            aria-label="Accounts"
            aria-busy={accounts.isFetching}
            footer={
              (cursor || accounts.data.nextCursor) && (
                <>
                  {cursor && (
                    <Button size="sm" variant="ghost" onClick={() => setCursor(undefined)}>
                      First page
                    </Button>
                  )}
                  {accounts.data.nextCursor && (
                    <Button size="sm" onClick={() => setCursor(accounts.data!.nextCursor!)}>
                      Next page
                    </Button>
                  )}
                </>
              )
            }
          >
            <DataTable
              label="Accounts"
              columns={columns}
              rows={items}
              rowKey={(account) => account.userId}
            />
          </Section>
        ) : (
          <div className="ui-card">
            <EmptyState title={query ? 'No matching accounts' : 'No accounts yet'} icon="search">
              {query
                ? 'Try a different name or username.'
                : 'Accounts appear here after people sign in or you create one.'}
            </EmptyState>
          </div>
        ))}
      <CreateAccountDialog open={creating} onClose={() => setCreating(false)} run={stepUp.run} />
      {current && (
        <ManageDialog
          key={current.userId}
          account={current}
          provider={provider}
          onClose={() => setManaging(null)}
          run={stepUp.run}
        />
      )}
      {stepUp.dialog}
    </Page>
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
      void client.invalidateQueries({ queryKey: ['accounts'] });
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

function ManageDialog({
  account,
  provider,
  onClose,
  run,
}: {
  account: Account;
  provider: string;
  onClose: () => void;
  run: ReturnType<typeof useStepUp>['run'];
}) {
  const [role, setRole] = useState(account.role);
  const [apps, setApps] = useState(account.appLimit);
  const [concurrent, setConcurrent] = useState(account.concurrencyLimit);
  const [link, setLink] = useState<{ title: string; url: string } | null>(null);
  const client = useQueryClient();
  const toast = useToast();
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
      void client.invalidateQueries({ queryKey: ['accounts'] });
    },
  });
  const quota = useMutation({
    mutationFn: () => adminApi.quotas(account.userId, apps, concurrent),
    onSuccess: () => {
      toast('Limits saved');
      void client.invalidateQueries({ queryKey: ['accounts'] });
    },
  });
  const state = accountState(account);
  const busy = change.isPending ? change.variables?.action : null;
  function action(name: string, value?: unknown) {
    change.mutate({ action: name, value });
  }
  return (
    <Dialog
      open
      onClose={onClose}
      title={account.displayName}
      size="lg"
      footer={<Button onClick={onClose}>Close</Button>}
    >
      <KeyValueList
        columns={2}
        items={[
          { label: 'Username', value: account.username },
          { label: 'Status', value: <Badge tone={state.tone}>{state.label}</Badge> },
          { label: 'Sign-in', value: local ? 'Local account' : provider },
          {
            label: 'Authenticator app',
            value: local ? (account.totpEnabled ? 'On' : 'Off') : '—',
          },
          { label: 'Last sign-in', value: <When value={account.lastSignIn} /> },
          { label: 'Account ID', value: <CopyId value={account.userId} label="account ID" /> },
        ]}
      />
      <ErrorAlert error={friendly(change.error) ?? quota.error} />
      {link && (
        <Alert tone="success" title={link.title}>
          <CopyField label="Setup link" value={link.url} hint={linkHint} />
        </Alert>
      )}
      <Group title="Role">
        <form
          className="ui-cluster ui-gap-2 admin-inline-form"
          onSubmit={(event) => {
            event.preventDefault();
            action('role', role);
          }}
        >
          <Field label={`Role for ${account.username}`} id={`role-${account.userId}`}>
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
      </Group>
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
      <Group title="Access">
        <div className="ui-cluster ui-gap-2">
          {local && account.status === 'pending' && (
            <Button loading={busy === 'invite'} onClick={() => action('invite')}>
              New invitation link
            </Button>
          )}
          {local && (
            <>
              <Button loading={busy === 'password-reset'} onClick={() => action('password-reset')}>
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
            onClick={() => action('enabled', !account.enabled)}
          >
            {account.enabled ? 'Disable account' : 'Enable account'}
          </Button>
        </div>
      </Group>
    </Dialog>
  );
}

const auditLabels: Record<string, string> = {
  account_invited: 'Account created',
  enrollment_started: 'Setup started',
  enrollment_completed: 'Setup finished',
  step_up: 'Identity confirmed',
  role: 'Role changed',
  enabled: 'Account enabled or disabled',
  'revoke-sessions': 'Signed out everywhere',
  'password-reset': 'Password reset link',
  'totp-reset': 'Authenticator reset link',
  invite: 'New invitation link',
  quotas: 'Limits changed',
  app_adopted: 'App adopted',
  app_owner_changed: 'Owner changed',
  app_deploy: 'App deployed',
  app_state: 'App started or stopped',
  app_storage_delete: 'Storage deleted',
  app_storage_create: 'Storage added',
  app_storage_rotate: 'Storage credentials rotated',
  app_storage_verify: 'Storage checked',
  app_env_set: 'Environment variable set',
  app_env_delete: 'Environment variable deleted',
  app_configuration: 'Settings saved',
};

function auditLabel(row: AdminAudit) {
  if (row.action === 'enabled' && typeof row.details.enabled === 'boolean')
    return row.details.enabled ? 'Account enabled' : 'Account disabled';
  return (
    auditLabels[row.action] ??
    row.action
      .replace(/^app_/, 'App: ')
      .replace(/[_-]/g, ' ')
      .replace(/^./, (c) => c.toUpperCase())
  );
}

function auditDetails(row: AdminAudit) {
  const parts: string[] = [];
  const { role, previousRole, purpose, apps, concurrentOperations } = row.details;
  if (typeof previousRole === 'string' && typeof role === 'string')
    parts.push(
      `${roleNames[previousRole as Account['role']] ?? previousRole} → ${roleNames[role as Account['role']] ?? role}`,
    );
  else if (typeof role === 'string') parts.push(roleNames[role as Account['role']] ?? role);
  if (typeof purpose === 'string') parts.push(auditLabels[purpose] ?? purpose);
  if (typeof apps === 'number') parts.push(`${apps} apps`);
  if (typeof concurrentOperations === 'number') parts.push(`${concurrentOperations} at a time`);
  return parts.join(' · ');
}

export function AdminAuditPage() {
  const [cursor, setCursor] = useState<string>();
  const names = useAccountNames();
  const audit = useQuery({
    queryKey: ['admin-audit', cursor],
    queryFn: () => adminApi.audit(cursor),
    retry: false,
  });
  const columns: Column<AdminAudit>[] = [
    { key: 'action', header: 'Action', mobile: 'title', cell: (row) => auditLabel(row) },
    {
      key: 'when',
      header: 'When',
      mobile: 'trailing',
      cell: (row) => <When value={row.createdAt} />,
    },
    {
      key: 'target',
      header: 'Account',
      cell: (row) => <AccountName id={row.targetId} names={names} />,
    },
    {
      key: 'actor',
      header: 'By',
      cell: (row) =>
        row.actorId && row.actorId === row.targetId ? (
          <span className="ui-text-muted">Themselves</span>
        ) : (
          <AccountName id={row.actorId} names={names} />
        ),
    },
    {
      key: 'details',
      header: 'Details',
      cell: (row) => {
        const text = auditDetails(row);
        return text ? (
          <span className="ui-text-muted">{text}</span>
        ) : (
          <span className="ui-text-subtle">—</span>
        );
      },
    },
  ];
  if (audit.isPending) return <PageSkeleton />;
  return (
    <Page>
      <PageHeader title="Audit log" />
      <ErrorAlert error={audit.error} />
      {audit.data &&
        (audit.data.items.length ? (
          <Section
            flush
            aria-label="Audit log"
            footer={
              (cursor || audit.data.nextCursor) && (
                <>
                  {cursor && (
                    <Button size="sm" variant="ghost" onClick={() => setCursor(undefined)}>
                      Newest
                    </Button>
                  )}
                  {audit.data.nextCursor && (
                    <Button size="sm" onClick={() => setCursor(audit.data!.nextCursor!)}>
                      Older
                    </Button>
                  )}
                </>
              )
            }
          >
            <DataTable
              label="Audit log"
              columns={columns}
              rows={audit.data.items}
              rowKey={(row) => String(row.id)}
            />
          </Section>
        ) : (
          <div className="ui-card">
            <EmptyState title="Nothing logged yet">
              Account and app changes made by admins appear here.
            </EmptyState>
          </div>
        ))}
    </Page>
  );
}
