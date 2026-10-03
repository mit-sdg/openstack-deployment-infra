import {
  ErrorAlert,
  KeyValueList,
  Page,
  PageHeader,
  PageSkeleton,
  Section,
} from '@openstack-platform/ui';
import { useState } from 'react';
import { staffApi } from '../../staffApi';
import {
  AppLink,
  Back,
  CopyId,
  DeploymentStatus,
  DeploymentTable,
  Loaded,
  pager,
  Refresh,
  When,
  useCachedApp,
  useRead,
} from './common';

const cleanup: Record<string, string> = {
  confirmed: 'Done',
  not_required: 'Not needed',
  pending: 'Pending',
  unknown: 'Unknown',
};

export function StaffHistory({ id }: { id: string }) {
  const [cursor, setCursor] = useState<string>();
  const history = useRead(['history', id, cursor], (signal) =>
    staffApi.deployments(id, cursor, signal),
  );
  // Name and live deployment come from the app page's cache when possible.
  const app = useCachedApp(id, !history.isPending);
  return (
    <Page>
      <PageHeader
        title="Deployments"
        back={<Back href={`/staff/apps/${id}`}>{app.data?.slug ?? 'App'}</Back>}
        actions={<Refresh queries={[history]} />}
      />
      <Section flush aria-label="Deployments" footer={pager(history.data, cursor, setCursor)}>
        <Loaded query={history}>
          {(page) => (
            <DeploymentTable app={id} live={app.data?.activeDeploymentId} rows={page.items} />
          )}
        </Loaded>
      </Section>
    </Page>
  );
}

export function StaffDeploymentPage({ id, deployment }: { id: string; deployment: string }) {
  const result = useRead(
    ['deployment', id, deployment],
    (signal) => staffApi.deployment(id, deployment, signal),
    { poll: true },
  );
  const app = useCachedApp(id, !result.isPending);
  const back = <Back href={`/staff/apps/${id}/deployments`}>Deployments</Back>;
  if (result.isPending) return <PageSkeleton />;
  if (result.error)
    return (
      <Page>
        <PageHeader title="Deployment" back={back} actions={<Refresh queries={[result]} />} />
        <ErrorAlert error={result.error} />
      </Page>
    );
  const data = result.data;
  return (
    <Page>
      <PageHeader
        title="Deployment"
        back={back}
        meta={<DeploymentStatus status={data.status} />}
        actions={<Refresh queries={[result]} />}
      />
      <Section title="Details">
        <KeyValueList
          columns={2}
          items={[
            { label: 'App', value: <AppLink id={id} name={app.data?.slug} /> },
            {
              label: 'Commit',
              value: data.repositoryCommit ? (
                <CopyId value={data.repositoryCommit} label="commit" length={9} />
              ) : (
                <span className="ui-text-subtle">Unknown</span>
              ),
            },
            {
              label: 'Settings version',
              value: data.configurationRevision ?? <span className="ui-text-subtle">Unknown</span>,
            },
            { label: 'Started', value: <When value={data.requestedAt} /> },
            { label: 'Went live', value: <When value={data.acceptedAt} empty="Not live" /> },
            { label: 'Last healthy', value: <When value={data.lastHealthyAt} /> },
            { label: 'Updated', value: <When value={data.updatedAt} /> },
            { label: 'Cleanup', value: cleanup[data.cleanupState] ?? 'Unknown' },
            { label: 'Deployment ID', value: <CopyId value={deployment} label="deployment ID" /> },
          ]}
        />
      </Section>
    </Page>
  );
}
