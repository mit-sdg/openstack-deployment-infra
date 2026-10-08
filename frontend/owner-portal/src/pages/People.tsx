import {
  Badge,
  Cluster,
  CopyId,
  DataTable,
  EmptyState,
  Field,
  Input,
  Icon,
  KeyValueList,
  Page,
  PageHeader,
  PageHeaderSkeleton,
  PageSkeleton,
  Section,
  SectionSkeleton,
  backLinkClass,
  type Column,
} from '@openstack-platform/ui';
import { useState } from 'react';
import { Link, useLocation } from 'wouter';
import { classApi, type Person } from '../classApi';
import { OperationList, needsAttention } from '../components/Operation';
import { ActivityEmpty, ActivityItem, Loaded, pager, viewAll } from '../components/ClassRecords';
import { QueryError } from '../components/Feedback';
import { Status } from '../components/Status';
import { useRead, useFollow, DETAIL, LIST } from '../hooks/useClassReads';
import { AccountControls, CreateAccountAction } from '../components/AccountControls';
import { AppCatalog } from './AllApps';
import { useDebounced } from './admin/common';
function AccountState({ person }: { person: Person }) {
  return (
    <Cluster gap={2}>
      {person.role !== 'owner' && (
        <Badge tone="info" dot={false}>
          {person.role === 'admin' ? 'Admin' : 'Staff'}
        </Badge>
      )}
      <Status
        state={
          !person.portalEnabled ? 'disabled' : person.status === 'pending' ? 'pending' : 'active'
        }
      />
    </Cluster>
  );
}
export function PeoplePage({ admin }: { admin: boolean }) {
  const [, navigate] = useLocation();
  const [search, setSearch] = useState('');
  const q = useDebounced(search.trim());
  const [cursor, setCursor] = useState<string>();
  const people = useRead(['people', q, cursor], (signal) => classApi.people(cursor, signal, q), {
    poll: LIST,
  });
  const columns: Column<Person>[] = [
    {
      key: 'name',
      header: 'Name',
      mobile: 'title',
      cell: (person) => (
        <Link className="ui-link ui-link--plain" href={`/people/${person.ownerId}`}>
          {person.displayName}
        </Link>
      ),
    },
    {
      key: 'username',
      header: 'Username',
      mobile: 'secondary',
      cell: (person) => <span className="ui-text-muted">{person.username}</span>,
    },
    {
      key: 'account',
      header: 'Account',
      mobile: 'trailing',
      cell: (person) => <AccountState person={person} />,
    },
  ];
  return (
    <Page>
      <PageHeader title="People" actions={admin ? <CreateAccountAction /> : undefined} />
      <Field label="Search people" id="people-search" hint="Search by name or username.">
        <Input
          type="search"
          maxLength={64}
          value={search}
          onChange={(event) => {
            setSearch(event.target.value);
            setCursor(undefined);
          }}
        />
      </Field>
      <Section flush aria-label="People" footer={pager(people.data, cursor, setCursor)}>
        <Loaded query={people} what="people" rows={5}>
          {(data) => (
            <DataTable
              label="People"
              columns={columns}
              rows={data.items}
              rowKey={(person) => person.ownerId}
              onRowClick={(person) => navigate(`/people/${person.ownerId}`)}
              empty={
                <EmptyState title={q ? 'No matching people' : 'No people yet'}>
                  {q
                    ? 'Try another name or username.'
                    : 'People appear here after they sign in or an admin creates an account.'}
                </EmptyState>
              }
            />
          )}
        </Loaded>
      </Section>
    </Page>
  );
}
export function PersonPage({ id, admin }: { id: string; admin: boolean }) {
  const person = useRead(['person', id], (signal) => classApi.person(id, signal), { poll: DETAIL });
  const attention = useRead(
    ['person-attention', id],
    (signal) => classApi.activity(id, undefined, undefined, signal, true),
    { enabled: person.isSuccess },
  );
  const activity = useRead(
    ['person-activity', id],
    (signal) => classApi.activity(id, undefined, undefined, signal),
    { enabled: person.isSuccess && !attention.isPending },
  );
  useFollow(person, [attention, activity]);
  const back = (
    <Link className={backLinkClass} href="/people">
      <Icon name="arrow-left" />
      People
    </Link>
  );
  if (person.isPending)
    return (
      <PageSkeleton label="Loading person…">
        <PageHeaderSkeleton />
        <SectionSkeleton title rows={2} />
        <SectionSkeleton title variant="table" rows={3} />
      </PageSkeleton>
    );
  if (person.error)
    return (
      <Page>
        <PageHeader title="Person" back={back} />
        <QueryError query={person} what="this person" />
      </Page>
    );
  const data = person.data;
  return (
    <Page>
      <PageHeader title={data.displayName} back={back} meta={<AccountState person={data} />} />
      <Section title="Details" aria-label="Details">
        <KeyValueList
          columns={2}
          items={[
            { label: 'Username', value: data.username },
            {
              label: 'Apps',
              value:
                data.quota.apps.limit === null
                  ? String(data.quota.apps.used + data.quota.apps.reserved)
                  : `${data.quota.apps.used + data.quota.apps.reserved} of ${data.quota.apps.limit}`,
            },
            { label: 'Account ID', value: <CopyId value={id} label="account ID" /> },
          ]}
        />
      </Section>
      <AppCatalog ownerId={id} title="Apps" />
      <Section
        title="Needs attention"
        aria-label="Needs attention"
        flush
        actions={viewAll(`/activity?ownerId=${id}`, !!attention.data?.nextCursor)}
      >
        <Loaded query={attention} what="activity that needs attention">
          {(page) =>
            page.items.length ? (
              <OperationList label="Activity that needs attention">
                {page.items.map((item) => (
                  <ActivityItem key={item.intentId} item={item} showOwner={false} />
                ))}
              </OperationList>
            ) : (
              <EmptyState title="Nothing needs attention">
                Blocked changes will appear here with a Resume action.
              </EmptyState>
            )
          }
        </Loaded>
      </Section>
      <Section
        title="Recent activity"
        aria-label="Recent activity"
        flush
        actions={viewAll(
          `/activity?ownerId=${id}`,
          (activity.data?.items.length ?? 0) > 5 || !!activity.data?.nextCursor,
        )}
      >
        <Loaded query={activity} what="recent activity">
          {(page) =>
            page.items.length ? (
              <OperationList label="Recent activity">
                {page.items
                  .filter((item) => !needsAttention(item))
                  .slice(0, 5)
                  .map((item) => (
                    <ActivityItem key={item.intentId} item={item} showOwner={false} />
                  ))}
              </OperationList>
            ) : (
              <ActivityEmpty />
            )
          }
        </Loaded>
      </Section>
      {admin && <AccountControls id={id} />}
    </Page>
  );
}
