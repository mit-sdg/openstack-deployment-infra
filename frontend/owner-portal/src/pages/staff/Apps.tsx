import {
  Alert,
  BoundaryText,
  Cluster,
  CopyId,
  KeyValueList,
  Page,
  PageHeader,
  PageSkeleton,
  RelativeTime,
  Section,
  SectionSkeleton,
} from '@openstack-platform/ui';
import { Link, useSearch } from 'wouter';
import { QueryError } from '../../components/Feedback';
import { OperationList } from '../../components/Operation';
import { Status } from '../../components/Status';
import { staffApi } from '../../staffApi';
import { short } from '../../utils/presentation';
import {
  ActivityEmpty,
  ActivityItem,
  appState,
  Back,
  DETAIL,
  DeploymentTable,
  DetailHeaderSkeleton,
  FilterChip,
  isMissing,
  LIST,
  Loaded,
  Missing,
  OwnerLink,
  Repository,
  useFollow,
  useOwner,
  useRead,
  viewAll,
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
  const page = useAppPage(ownerId, { poll: LIST });
  const name = useOwnerName(ownerId, page.query.data?.items);
  return (
    <Page>
      <PageHeader title="All apps" />
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
  const app = useRead(['app', id], (signal) => staffApi.app(id, signal), { poll: DETAIL });
  const deployments = useRead(
    ['history', id, undefined],
    (signal) => staffApi.deployments(id, undefined, signal),
    { enabled: app.isSuccess },
  );
  const activity = useRead(
    ['operations', undefined, id, undefined],
    (signal) => staffApi.operations(undefined, id, undefined, signal),
    { enabled: app.isSuccess && !deployments.isPending },
  );
  useFollow(app, [deployments, activity]);
  const ownerName = app.data?.ownerDisplayName;
  const back = <Back href="/staff/apps">All apps</Back>;
  if (app.isPending)
    return (
      <PageSkeleton label="Loading app…">
        <DetailHeaderSkeleton back={back} meta extra />
        <SectionSkeleton title rows={3} />
        <SectionSkeleton title variant="table" columns={4} rows={1} />
        <SectionSkeleton title variant="list" density="compact" rows={3} />
      </PageSkeleton>
    );
  if (isMissing(app.error))
    return (
      <Missing title="App not found" href="/staff/apps" action="Go to all apps">
        It may have been deleted. Check the address, or go back to the app list.
      </Missing>
    );
  if (app.error)
    return (
      <Page>
        <PageHeader title="App" back={back} />
        <QueryError query={app} what="this app" />
      </Page>
    );
  const data = app.data;
  const { process, route } = data.health;
  // The title badge already says how the app is; split it only when the checks disagree.
  const split = data.lifecycleState === 'ready' && !data.stale && process !== route;
  return (
    <Page>
      <PageHeader title={data.slug} back={back} meta={<Status state={appState(data)} />}>
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
        <Alert tone="warning">Health is unknown right now. This page checks again shortly.</Alert>
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
            ...(data.members.length
              ? [
                  {
                    label: 'Team',
                    value: data.members.map((member) => member.displayName).join(', '),
                  },
                ]
              : []),
            { label: 'Created', value: <RelativeTime value={data.createdAt} /> },
            { label: 'Repository', value: <Repository url={data.repository} /> },
            { label: 'ID', value: <CopyId value={id} label="app ID" /> },
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
                    <RelativeTime value={data.acceptedDeployment.acceptedAt} />
                  </span>
                </Cluster>
              ) : (
                <span className="ui-text-subtle">Not deployed</span>
              ),
            },
            {
              label: 'Health checked',
              value: <RelativeTime value={data.observedAt} empty="Not checked yet" />,
            },
            ...(split
              ? [
                  { label: 'App', value: <Status state={process} /> },
                  { label: 'Public URL', value: <Status state={route} /> },
                ]
              : []),
          ]}
        />
      </Section>
      <Section
        title="Deployments"
        flush
        actions={viewAll(
          `/staff/apps/${id}/deployments`,
          (deployments.data?.items.length ?? 0) > 5,
        )}
      >
        <Loaded query={deployments} what="deployments" rows={1}>
          {(page) => (
            <DeploymentTable
              app={id}
              live={data.activeDeploymentId}
              rows={page.items.slice(0, 5)}
            />
          )}
        </Loaded>
      </Section>
      <Section
        title="Recent activity"
        flush
        actions={viewAll(
          `/staff/operations?applicationId=${id}`,
          (activity.data?.items.length ?? 0) > 5,
        )}
      >
        <Loaded query={activity} what="recent activity">
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
      </Section>
    </Page>
  );
}
