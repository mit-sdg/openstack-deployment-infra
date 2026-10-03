import { Field, Grid, List, Page, PageHeader, Section, Select } from '@openstack-platform/ui';
import { useState } from 'react';
import { useLocation, useSearch } from 'wouter';
import { staffApi } from '../../staffApi';
import { OwnerFilter } from './Apps';
import {
  ActivityEmpty,
  ActivityItem,
  Loaded,
  nameMap,
  pager,
  Refresh,
  useAppNames,
  useOwnerNames,
  useRead,
} from './common';

export function StaffOperations() {
  const search = useSearch();
  return <ActivityPage key={search} search={search} />;
}

function ActivityPage({ search }: { search: string }) {
  const params = new URLSearchParams(search);
  const ownerId = params.get('ownerId') ?? undefined;
  const applicationId = params.get('applicationId') ?? undefined;
  const [, navigate] = useLocation();
  const [cursor, setCursor] = useState<string>();
  const activity = useRead(
    ['operations', ownerId, applicationId, cursor],
    (signal) => staffApi.operations(ownerId, applicationId, cursor, signal),
    { poll: true },
  );
  const loaded = !activity.isPending;
  const ownerQuery = useOwnerNames(loaded);
  const appQuery = useAppNames(loaded && !ownerQuery.isPending);
  const owners = ownerQuery.data?.items ?? [];
  const apps = appQuery.data?.items ?? [];
  const appNames = nameMap(
    apps,
    (app) => app.applicationId,
    (app) => app.slug,
  );
  const ownerNames = nameMap(
    owners,
    (owner) => owner.ownerId,
    (owner) => owner.displayName,
  );
  const choices = apps.filter((app) => !ownerId || app.ownerId === ownerId);
  function filter(owner?: string, app?: string) {
    const next = new URLSearchParams();
    if (owner) next.set('ownerId', owner);
    if (app) next.set('applicationId', app);
    navigate(next.size ? `/staff/operations?${next}` : '/staff/operations');
  }
  return (
    <Page>
      <PageHeader title="Activity" actions={<Refresh queries={[activity]} />} />
      <Grid columns={3}>
        <OwnerFilter value={ownerId} owners={owners} onChange={(owner) => filter(owner)} />
        <Field label="App" id="staff-app-filter">
          <Select
            value={applicationId ?? ''}
            onChange={(event) => filter(ownerId, event.target.value || undefined)}
          >
            <option value="">All apps</option>
            {applicationId && !choices.some((app) => app.applicationId === applicationId) && (
              <option value={applicationId}>
                {apps.find((app) => app.applicationId === applicationId)?.slug ??
                  `App ${applicationId.slice(0, 8)}`}
              </option>
            )}
            {choices.map((app) => (
              <option key={app.applicationId} value={app.applicationId}>
                {app.slug}
              </option>
            ))}
          </Select>
        </Field>
      </Grid>
      <Section flush aria-label="Activity" footer={pager(activity.data, cursor, setCursor)}>
        <Loaded query={activity}>
          {(page) =>
            page.items.length ? (
              <List label="Activity">
                {page.items.map((item) => (
                  <ActivityItem
                    key={item.intentId}
                    item={item}
                    apps={applicationId ? undefined : appNames}
                    owners={ownerId || applicationId ? undefined : ownerNames}
                  />
                ))}
              </List>
            ) : (
              <ActivityEmpty filtered={!!(ownerId || applicationId)} />
            )
          }
        </Loaded>
      </Section>
    </Page>
  );
}
