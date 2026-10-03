import {
  Badge,
  DataTable,
  EmptyState,
  KeyValueList,
  List,
  Page,
  PageHeader,
  PageSkeleton,
  ErrorAlert,
  Section,
  type Column,
} from '@openstack-platform/ui';
import { useState } from 'react';
import { Link } from 'wouter';
import { staffApi, type StaffOwner } from '../../staffApi';
import {
  ActivityEmpty,
  ActivityItem,
  appColumns,
  Back,
  CopyId,
  Loaded,
  nameMap,
  pager,
  Preview,
  Refresh,
  useRead,
} from './common';

function Access({ enabled }: { enabled: boolean }) {
  return enabled ? <Badge tone="success">Active</Badge> : <Badge>Disabled</Badge>;
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
    key: 'status',
    header: 'Status',
    mobile: 'trailing',
    cell: (owner) => <Access enabled={owner.portalEnabled} />,
  },
  {
    key: 'username',
    header: 'Username',
    cell: (owner) => <span className="ui-text-muted">{owner.username}</span>,
  },
];

export function StaffOwners() {
  const [cursor, setCursor] = useState<string>();
  const owners = useRead(['owners', cursor], (signal) => staffApi.owners(cursor, signal));
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
  owners,
  title,
  filtered,
}: {
  page: ReturnType<typeof useAppPage>;
  owners?: Map<string, string>;
  title?: string;
  filtered?: boolean;
}) {
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
            columns={appColumns(owners)}
            rows={page.items}
            rowKey={(app) => app.applicationId}
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
  const apps = nameMap(
    catalog.query.data?.items ?? [],
    (app) => app.applicationId,
    (app) => app.slug,
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
        meta={<Access enabled={data.portalEnabled} />}
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
              <List label="Recent activity">
                {page.items.slice(0, 5).map((item) => (
                  <ActivityItem key={item.intentId} item={item} apps={apps} />
                ))}
              </List>
            ) : (
              <ActivityEmpty />
            )
          }
        </Loaded>
      </Preview>
    </Page>
  );
}
