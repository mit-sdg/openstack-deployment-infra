import {
  Badge,
  Cluster,
  CopyId,
  DataTable,
  EmptyState,
  KeyValueList,
  Page,
  PageHeader,
  PageSkeleton,
  Section,
  SectionSkeleton,
  type Column,
} from '@openstack-platform/ui';
import { useState } from 'react';
import { Link, useLocation } from 'wouter';
import { QueryError } from '../../components/Feedback';
import { OperationList } from '../../components/Operation';
import { Status } from '../../components/Status';
import { staffApi, type StaffOwner } from '../../staffApi';
import {
  ActivityEmpty,
  ActivityItem,
  appColumns,
  Back,
  DETAIL,
  DetailHeaderSkeleton,
  isMissing,
  LIST,
  Loaded,
  Missing,
  pager,
  useFollow,
  useRead,
  viewAll,
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
      {!owner.portalEnabled && <Status state="disabled" />}
    </Cluster>
  );
}

const ownerColumns: Column<StaffOwner>[] = [
  {
    key: 'name',
    header: 'Name',
    mobile: 'title',
    cell: (owner) => (
      <Link href={`/staff/owners/${owner.ownerId}`} className="ui-link ui-link--plain">
        {owner.displayName}
      </Link>
    ),
  },
  {
    key: 'username',
    header: 'Username',
    mobile: 'secondary',
    cell: (owner) => <span className="ui-text-muted">{owner.username}</span>,
  },
  {
    key: 'account',
    header: 'Account',
    hideHeader: true,
    mobile: 'trailing',
    cell: (owner) => <AccountBadges owner={owner} />,
  },
];

/** Owner accounts only: the server leaves out staff and admins. */
export function StaffOwners() {
  const [cursor, setCursor] = useState<string>();
  const owners = useRead(['owners', cursor], (signal) => staffApi.owners(cursor, signal, 'owner'), {
    poll: LIST,
  });
  const [, navigate] = useLocation();
  return (
    <Page>
      <PageHeader title="Owners" />
      <Section flush aria-label="Owners" footer={pager(owners.data, cursor, setCursor)}>
        <Loaded query={owners} what="owners" rows={5}>
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
export function useAppPage(ownerId?: string, { enabled = true, poll = 0 } = {}) {
  const [cursor, setCursor] = useState<string>();
  const query = useRead(
    ['apps', ownerId, cursor],
    (signal) => staffApi.apps(ownerId, cursor, signal),
    { enabled, poll },
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
      <Loaded query={query} what="apps" rows={title ? 1 : 5}>
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
  const owner = useRead(['owner', id], (signal) => staffApi.owner(id, signal), { poll: DETAIL });
  const catalog = useAppPage(id, { enabled: owner.isSuccess });
  const activity = useRead(
    ['operations', id, undefined, undefined],
    (signal) => staffApi.operations(id, undefined, undefined, signal),
    { enabled: owner.isSuccess && !catalog.query.isPending },
  );
  useFollow(owner, [catalog.query, activity]);
  const back = <Back href="/staff/owners">Owners</Back>;
  if (owner.isPending)
    return (
      <PageSkeleton label="Loading owner…">
        <DetailHeaderSkeleton back={back} />
        <SectionSkeleton title rows={2} />
        <SectionSkeleton title variant="table" columns={3} rows={1} />
        <SectionSkeleton title variant="list" density="compact" rows={3} />
      </PageSkeleton>
    );
  if (isMissing(owner.error))
    return (
      <Missing title="Owner not found" href="/staff/owners" action="Go to owners">
        Check the address, or go back to the owner list.
      </Missing>
    );
  if (owner.error)
    return (
      <Page>
        <PageHeader title="Owner" back={back} />
        <QueryError query={owner} what="this owner" />
      </Page>
    );
  const data = owner.data;
  const apps = data.quota.apps;
  return (
    <Page>
      <PageHeader title={data.displayName} back={back} meta={<AccountBadges owner={data} />} />
      <Section title="Details">
        <KeyValueList
          columns={2}
          items={[
            { label: 'Username', value: data.username },
            { label: 'Apps', value: `${apps.used + apps.reserved} of ${apps.limit}` },
            { label: 'ID', value: <CopyId value={id} label="owner ID" /> },
          ]}
        />
      </Section>
      <AppsSection page={catalog} title="Apps" filtered />
      <Section
        title="Recent activity"
        flush
        actions={viewAll(`/staff/operations?ownerId=${id}`, (activity.data?.items.length ?? 0) > 5)}
      >
        <Loaded query={activity} what="recent activity">
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
      </Section>
    </Page>
  );
}
