import {
  Badge,
  Cluster,
  DataTable,
  EmptyState,
  KeyValueList,
  Page,
  PageHeader,
  PageSkeleton,
  ErrorAlert,
  Section,
  type Column,
} from '@openstack-platform/ui';
import { useState } from 'react';
import { Link, useLocation } from 'wouter';
import { OperationList } from '../../components/Operation';
import { staffApi, type StaffOwner } from '../../staffApi';
import {
  ActivityEmpty,
  ActivityItem,
  appColumns,
  Back,
  CopyId,
  Loaded,
  PhoneDetail,
  pager,
  Preview,
  Refresh,
  useRead,
} from './common';

const roles = { staff: 'Staff', admin: 'Admin' } as const;

/** Badges only for what stands out: a staff or admin role, or a disabled account. */
function AccountBadges({ owner }: { owner: StaffOwner }) {
  if (owner.role === 'owner' && owner.portalEnabled) return null;
  return (
    <Cluster gap={2}>
      {owner.role !== 'owner' && (
        <Badge tone="info" dot={false}>
          {roles[owner.role]}
        </Badge>
      )}
      {!owner.portalEnabled && <Badge tone="warning">Disabled</Badge>}
    </Cluster>
  );
}

const ownerColumns: Column<StaffOwner>[] = [
  {
    key: 'name',
    header: 'Name',
    mobile: 'title',
    cell: (owner) => (
      <>
        <Link href={`/staff/owners/${owner.ownerId}`} className="ui-link ui-link--plain">
          {owner.displayName}
        </Link>
        <PhoneDetail>{owner.username}</PhoneDetail>
      </>
    ),
  },
  {
    key: 'username',
    header: 'Username',
    mobile: 'hidden',
    cell: (owner) => <span className="ui-text-muted">{owner.username}</span>,
  },
  {
    key: 'account',
    header: 'Account',
    mobile: 'trailing',
    cell: (owner) => <AccountBadges owner={owner} />,
  },
];

export function StaffOwners() {
  const [cursor, setCursor] = useState<string>();
  const owners = useRead(['owners', cursor], (signal) => staffApi.owners(cursor, signal));
  const [, navigate] = useLocation();
  return (
    <Page>
      <PageHeader title="Owners" actions={<Refresh queries={[owners]} />} />
      <Section flush aria-label="Owners" footer={pager(owners.data, cursor, setCursor)}>
        <Loaded query={owners}>
          {(page) => (
            <DataTable
              label="Owners"
              columns={ownerColumns}
              rows={page.items}
              rowKey={(owner) => owner.ownerId}
              onRowClick={(owner) => navigate(`/staff/owners/${owner.ownerId}`)}
              empty={
                <EmptyState title="No owners yet">
                  People appear here after they first sign in.
                </EmptyState>
              }
            />
          )}
        </Loaded>
      </Section>
    </Page>
  );
}

/** One page of apps for an owner, or of every app. */
export function useAppPage(ownerId?: string, enabled = true) {
  const [cursor, setCursor] = useState<string>();
  const query = useRead(
    ['apps', ownerId, cursor],
    (signal) => staffApi.apps(ownerId, cursor, signal),
    { enabled },
  );
  return { query, cursor, setCursor };
}

export function AppsSection({
  page: { query, cursor, setCursor },
  showOwner = false,
  title,
  filtered,
}: {
  page: ReturnType<typeof useAppPage>;
  showOwner?: boolean;
  title?: string;
  filtered?: boolean;
}) {
  const [, navigate] = useLocation();
  return (
    <Section
      title={title}
      aria-label={title ? undefined : 'Apps'}
      flush
      footer={pager(query.data, cursor, setCursor)}
    >
      <Loaded query={query}>
        {(page) => (
          <DataTable
            label="Apps"
            columns={appColumns(showOwner)}
            rows={page.items}
            rowKey={(app) => app.applicationId}
            onRowClick={(app) => navigate(`/staff/apps/${app.applicationId}`)}
            empty={
              <EmptyState title="No apps yet">
                {filtered
                  ? 'Apps this owner creates will appear here.'
                  : 'Apps will appear here once owners create them.'}
              </EmptyState>
            }
          />
        )}
      </Loaded>
    </Section>
  );
}

export function StaffOwnerPage({ id }: { id: string }) {
  const owner = useRead(['owner', id], (signal) => staffApi.owner(id, signal));
  const catalog = useAppPage(id, !owner.isPending);
  const activity = useRead(
    ['operations', id, undefined, undefined],
    (signal) => staffApi.operations(id, undefined, undefined, signal),
    { poll: true, enabled: !catalog.query.isPending },
  );
  if (owner.isPending) return <PageSkeleton />;
  const back = <Back href="/staff/owners">Owners</Back>;
  if (owner.error)
    return (
      <Page>
        <PageHeader title="Owner" back={back} actions={<Refresh queries={[owner]} />} />
        <ErrorAlert error={owner.error} />
      </Page>
    );
  const data = owner.data;
  const quota = (kind: keyof typeof data.quota) =>
    `${data.quota[kind].used + data.quota[kind].reserved} of ${data.quota[kind].limit}`;
  return (
    <Page>
      <PageHeader
        title={data.displayName}
        back={back}
        meta={<AccountBadges owner={data} />}
        actions={<Refresh queries={[owner, catalog.query, activity]} />}
      />
      <Section title="Details">
        <KeyValueList
          columns={2}
          items={[
            { label: 'Username', value: data.username },
            { label: 'Apps', value: quota('apps') },
            { label: 'Owner ID', value: <CopyId value={id} label="owner ID" /> },
            { label: 'Changes in progress', value: quota('concurrentOperations') },
          ]}
        />
      </Section>
      <AppsSection page={catalog} title="Apps" filtered />
      <Preview
        title="Recent activity"
        href={`/staff/operations?ownerId=${id}`}
        more={(activity.data?.items.length ?? 0) > 5}
      >
        <Loaded query={activity}>
          {(page) =>
            page.items.length ? (
              <OperationList label="Recent activity">
                {page.items.slice(0, 5).map((item) => (
                  <ActivityItem key={item.intentId} item={item} showOwner={false} />
                ))}
              </OperationList>
            ) : (
              <ActivityEmpty />
            )
          }
        </Loaded>
      </Preview>
    </Page>
  );
}
