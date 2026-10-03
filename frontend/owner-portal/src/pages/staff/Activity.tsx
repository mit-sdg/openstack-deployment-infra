import { Page, PageHeader, Section } from '@openstack-platform/ui';
import { useState } from 'react';
import { useSearch } from 'wouter';
import { OperationList } from '../../components/Operation';
import { staffApi } from '../../staffApi';
import { useOwnerName } from './Apps';
import {
  ActivityEmpty,
  ActivityItem,
  AppLink,
  FilterChip,
  Loaded,
  OwnerLink,
  pager,
  Refresh,
  useRead,
} from './common';

export function StaffOperations() {
  const search = useSearch();
  return <ActivityPage key={search} search={search} />;
}

// Filters come from links on owner and app pages (?ownerId=, ?applicationId=).
function ActivityPage({ search }: { search: string }) {
  const params = new URLSearchParams(search);
  const ownerId = params.get('ownerId') ?? undefined;
  const applicationId = params.get('applicationId') ?? undefined;
  const [cursor, setCursor] = useState<string>();
  const activity = useRead(
    ['operations', ownerId, applicationId, cursor],
    (signal) => staffApi.operations(ownerId, applicationId, cursor, signal),
    { poll: true },
  );
  const rows = activity.data?.items;
  const ownerName = useOwnerName(ownerId, rows);
  const appName = rows?.[0]?.applicationSlug;
  return (
    <Page>
      <PageHeader title="Activity" actions={<Refresh queries={[activity]} />} />
      {ownerId && (
        <FilterChip
          label="Owner"
          value={<OwnerLink id={ownerId} name={ownerName ?? `Owner ${ownerId.slice(0, 8)}`} />}
          clear="/staff/operations"
        />
      )}
      {applicationId && (
        <FilterChip
          label="App"
          value={
            <AppLink id={applicationId} name={appName ?? `App ${applicationId.slice(0, 8)}`} />
          }
          clear="/staff/operations"
        />
      )}
      <Section flush aria-label="Activity" footer={pager(activity.data, cursor, setCursor)}>
        <Loaded query={activity}>
          {(page) =>
            page.items.length ? (
              <OperationList label="Activity">
                {page.items.map((item) => (
                  <ActivityItem
                    key={item.intentId}
                    item={item}
                    showApp={!applicationId}
                    showOwner={!ownerId && !applicationId}
                  />
                ))}
              </OperationList>
            ) : (
              <ActivityEmpty filtered={!!(ownerId || applicationId)} />
            )
          }
        </Loaded>
      </Section>
    </Page>
  );
}
