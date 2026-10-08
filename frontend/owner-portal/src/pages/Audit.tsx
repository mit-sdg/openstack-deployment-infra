import {
  Button,
  CopyId,
  DataTable,
  EmptyState,
  Page,
  PageHeader,
  PageHeaderSkeleton,
  PageSkeleton,
  RelativeTime,
  Section,
  SectionSkeleton,
  type Column,
} from '@openstack-platform/ui';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { adminApi, type Account, type AdminAudit } from '../adminApi';
import { QueryError } from '../components/Feedback';
import { roleNames } from './admin/common';
function When({ value }: { value: string | null }) {
  return (
    <span className="ui-text-muted">
      <RelativeTime value={value} empty="Never" />
    </span>
  );
}
const auditLabels: Record<string, string> = {
  default_builder_size_requested: 'Default build machine change started',
  default_builder_size_succeeded: 'Default build machine changed',
  default_builder_size_failed: 'Default build machine change failed',
  default_builder_size_blocked: 'Default build machine change needs attention',
  app_builder_size: 'Build machine change started',
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
  app_adopt_app: 'App import started',
  app_create_app: 'App created',
  app_deploy: 'App deployed',
  app_resume: 'Change resumed',
  app_state: 'App started or stopped',
  app_storage: 'Storage added',
  app_storage_delete: 'Storage deleted',
  app_storage_create: 'Storage added',
  app_storage_rotate: 'Storage credentials rotated',
  app_storage_verify: 'Storage checked',
  app_env_set: 'Environment variable set',
  app_env_delete: 'Environment variable deleted',
  app_configuration: 'Settings saved',
};

const purposes: Record<string, string> = {
  bootstrap: 'First admin',
  invite: 'Invitation',
  'password-reset': 'Password reset',
  'totp-reset': 'Authenticator reset',
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
  if (typeof purpose === 'string') parts.push(purposes[purpose] ?? purpose);
  if (typeof apps === 'number') parts.push(`${apps} apps`);
  if (typeof concurrentOperations === 'number') parts.push(`${concurrentOperations} at a time`);
  if (typeof row.details.flavor === 'string')
    parts.push(`${row.details.expectedFlavor ?? 'Platform default'} → ${row.details.flavor}`);
  return parts.join(' · ');
}

/** A person in the log: their name, with the username as the tooltip. */
function Person({
  id,
  username,
  displayName,
}: {
  id: string | null;
  username: string | null;
  displayName: string | null;
}) {
  if (!id) return <span className="ui-text-subtle">—</span>;
  if (!displayName) return <CopyId value={id} label="account ID" />;
  return <span title={username ?? undefined}>{displayName}</span>;
}

export function AdminAuditPage() {
  const [cursor, setCursor] = useState<string>();
  const audit = useQuery({
    queryKey: ['admin-audit', cursor],
    queryFn: () => adminApi.audit(cursor),
    retry: false,
  });
  const target = (row: AdminAudit) => (
    <Person id={row.targetId} username={row.targetUsername} displayName={row.targetDisplayName} />
  );
  const columns: Column<AdminAudit>[] = [
    { key: 'action', header: 'Action', mobile: 'title', cell: (row) => auditLabel(row) },
    { key: 'target', header: 'Account', mobile: 'secondary', cell: target },
    {
      key: 'actor',
      header: 'By',
      mobile: 'hidden',
      cell: (row) => (
        <Person id={row.actorId} username={row.actorUsername} displayName={row.actorDisplayName} />
      ),
    },
    {
      key: 'details',
      header: 'Details',
      mobile: 'meta',
      cell: (row) => {
        const text = auditDetails(row);
        const app = row.details.applicationId;
        return typeof app === 'string' ? (
          <span className="ui-cluster ui-gap-2">
            <span className="ui-text-muted">App</span>
            <CopyId value={app} label="app ID" />
          </span>
        ) : text ? (
          <span className="ui-text-muted">{text}</span>
        ) : (
          <span className="ui-text-subtle">—</span>
        );
      },
    },
    {
      key: 'when',
      header: 'When',
      mobile: 'trailing',
      cell: (row) => <When value={row.createdAt} />,
    },
  ];
  if (audit.isPending)
    return (
      <PageSkeleton label="Loading the audit log…">
        <PageHeaderSkeleton />
        <SectionSkeleton variant="table" columns={5} rows={8} />
      </PageSkeleton>
    );
  return (
    <Page>
      <PageHeader title="Audit log" />
      {audit.error && <QueryError query={audit} what="the audit log" />}
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
              Account changes and app changes by staff and admins appear here.
            </EmptyState>
          </div>
        ))}
    </Page>
  );
}
