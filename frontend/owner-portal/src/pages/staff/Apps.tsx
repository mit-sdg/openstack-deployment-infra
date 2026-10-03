import {
  Alert,
  BoundaryText,
  Cluster,
  ErrorAlert,
  KeyValueList,
  Page,
  PageHeader,
  PageSkeleton,
  Section,
} from '@openstack-platform/ui';
import { Link, useSearch } from 'wouter';
import { OperationList } from '../../components/Operation';
import { staffApi } from '../../staffApi';
import { short } from '../../utils/presentation';
import {
  ActivityEmpty,
  ActivityItem,
  AppHealth,
  Back,
  CopyId,
  DeploymentTable,
  Loaded,
  FilterChip,
  HealthValue,
  OwnerLink,
  Preview,
  Refresh,
  Repository,
  When,
  useOwner,
  useRead,
} from './common';
import { AppsSection, useAppPage } from './Owners';

/** An owner's name for a filter: from the listed rows, else one owner read. */
export function useOwnerName(
  id: string | undefined,
  rows: { ownerDisplayName: string }[] | undefined,
) {
  const lookup = useOwner(id, !!rows && !rows.length);
  return rows?.[0]?.ownerDisplayName ?? lookup.data?.displayName;
}

export function StaffApps() {
  const ownerId = new URLSearchParams(useSearch()).get('ownerId') ?? undefined;
  const page = useAppPage(ownerId);
  const name = useOwnerName(ownerId, page.query.data?.items);
  return (
    <Page>
      <PageHeader title="All apps" actions={<Refresh queries={[page.query]} />} />
      {ownerId && (
        <FilterChip
          label="Owner"
          value={<OwnerLink id={ownerId} name={name ?? `Owner ${ownerId.slice(0, 8)}`} />}
          clear="/staff/apps"
        />
      )}
      <AppsSection key={ownerId ?? 'all'} page={page} filtered={!!ownerId} showOwner />
    </Page>
  );
}

export function StaffAppPage({ id }: { id: string }) {
  const app = useRead(['app', id], (signal) => staffApi.app(id, signal), { poll: true });
  const deployments = useRead(
    ['history', id, undefined],
    (signal) => staffApi.deployments(id, undefined, signal),
    { enabled: app.isSuccess },
  );
  const activity = useRead(
    ['operations', undefined, id, undefined],
    (signal) => staffApi.operations(undefined, id, undefined, signal),
    { poll: true, enabled: app.isSuccess && !deployments.isPending },
  );
  const ownerName = useOwnerName(app.data?.ownerId, activity.data?.items);
  const back = <Back href="/staff/apps">All apps</Back>;
  if (app.isPending) return <PageSkeleton />;
  if (app.error)
    return (
      <Page>
        <PageHeader title="App" back={back} actions={<Refresh queries={[app]} />} />
        <ErrorAlert error={app.error} />
      </Page>
    );
  const data = app.data;
  return (
    <Page>
      <PageHeader
        title={data.slug}
        back={back}
        meta={<AppHealth app={data} />}
        actions={<Refresh queries={[app, deployments, activity]} />}
      >
        {data.url && (
          <a
            className="ui-link ui-text-sm"
            href={data.url}
            target="_blank"
            rel="noopener noreferrer"
          >
            <BoundaryText text={new URL(data.url).hostname} />
          </a>
        )}
      </PageHeader>
      {data.stale && data.lifecycleState === 'ready' && (
        <Alert tone="warning">
          Health information is out of date. It updates every 15 seconds.
        </Alert>
      )}
      <Section title="Details">
        <KeyValueList
          columns={2}
          items={[
            {
              label: 'Owner',
              value: ownerName ? (
                <OwnerLink id={data.ownerId} name={ownerName} />
              ) : (
                <CopyId value={data.ownerId} label="owner ID" />
              ),
            },
            { label: 'Created', value: <When value={data.createdAt} /> },
            { label: 'Repository', value: <Repository url={data.repository} /> },
            { label: 'App ID', value: <CopyId value={id} label="app ID" /> },
            {
              label: 'Deployed commit',
              value: data.acceptedDeployment ? (
                <Cluster gap={2}>
                  <Link
                    href={`/staff/apps/${id}/deployments/${data.acceptedDeployment.deploymentId}`}
                    className="ui-link"
                  >
                    <code>{short(data.acceptedDeployment.sourceCommit)}</code>
                  </Link>
                  <span className="ui-text-muted">
                    <When value={data.acceptedDeployment.acceptedAt} />
                  </span>
                </Cluster>
              ) : (
                <span className="ui-text-subtle">Not deployed</span>
              ),
            },
            {
              label: 'Health checked',
              value: <When value={data.observedAt} empty="Not checked yet" />,
            },
            { label: 'App health', value: <HealthValue state={data.health.process} /> },
            { label: 'URL health', value: <HealthValue state={data.health.route} /> },
          ]}
        />
      </Section>
      <Preview
        title="Deployments"
        href={`/staff/apps/${id}/deployments`}
        more={(deployments.data?.items.length ?? 0) > 5}
      >
        <Loaded query={deployments}>
          {(page) => (
            <DeploymentTable
              app={id}
              live={data.activeDeploymentId}
              rows={page.items.slice(0, 5)}
            />
          )}
        </Loaded>
      </Preview>
      <Preview
        title="Recent activity"
        href={`/staff/operations?applicationId=${id}`}
        more={(activity.data?.items.length ?? 0) > 5}
      >
        <Loaded query={activity}>
          {(page) =>
            page.items.length ? (
              <OperationList label="Recent activity">
                {page.items.slice(0, 5).map((item) => (
                  <ActivityItem key={item.intentId} item={item} showApp={false} showOwner={false} />
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
