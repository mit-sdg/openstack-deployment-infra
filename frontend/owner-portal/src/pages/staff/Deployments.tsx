import {
  CopyId,
  KeyValueList,
  Page,
  PageHeader,
  PageSkeleton,
  RelativeTime,
  Section,
  SectionSkeleton,
} from '@openstack-platform/ui';
import { useState } from 'react';
import { QueryError } from '../../components/Feedback';
import { Status } from '../../components/Status';
import { staffApi } from '../../staffApi';
import {
  AppLink,
  Back,
  DETAIL,
  DeploymentTable,
  DetailHeaderSkeleton,
  duration,
  isMissing,
  LIST,
  Loaded,
  Missing,
  pager,
  useCachedApp,
  useRead,
} from './common';

export function StaffHistory({ id }: { id: string }) {
  const [cursor, setCursor] = useState<string>();
  const history = useRead(
    ['history', id, cursor],
    (signal) => staffApi.deployments(id, cursor, signal),
    { poll: LIST },
  );
  // Name and live deployment come from the app page's cache when possible.
  const app = useCachedApp(id, !history.isPending);
  if (isMissing(history.error))
    return (
      <Missing title="App not found" href="/staff/apps" action="Go to all apps">
        It may have been deleted. Check the address, or go back to the app list.
      </Missing>
    );
  return (
    <Page>
      <PageHeader
        title="Deployments"
        back={<Back href={`/staff/apps/${id}`}>{app.data?.slug ?? 'App'}</Back>}
      />
      <Section flush aria-label="Deployments" footer={pager(history.data, cursor, setCursor)}>
        <Loaded query={history} what="deployments">
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
    { poll: DETAIL },
  );
  const app = useCachedApp(id, !result.isPending);
  const back = <Back href={`/staff/apps/${id}/deployments`}>Deployments</Back>;
  if (result.isPending)
    return (
      <PageSkeleton label="Loading deployment…">
        <DetailHeaderSkeleton back={back} meta />
        <SectionSkeleton title rows={3} />
      </PageSkeleton>
    );
  if (isMissing(result.error))
    return (
      <Missing
        title="Deployment not found"
        href={`/staff/apps/${id}/deployments`}
        action="Go to deployments"
      >
        Check the address, or go back to this app's deployments.
      </Missing>
    );
  if (result.error)
    return (
      <Page>
        <PageHeader title="Deployment" back={back} />
        <QueryError query={result} what="this deployment" />
      </Page>
    );
  const data = result.data;
  const took = duration(data);
  return (
    <Page>
      <PageHeader title="Deployment" back={back} meta={<Status state={data.status} />} />
      <Section title="Details">
        <KeyValueList
          columns={2}
          items={[
            {
              label: 'App',
              value: <AppLink id={id} name={app.data?.slug ?? `App ${id.slice(0, 8)}`} />,
            },
            {
              label: 'Commit',
              value: data.repositoryCommit ? (
                <CopyId value={data.repositoryCommit} label="commit" length={9} />
              ) : (
                <span className="ui-text-subtle">Unknown</span>
              ),
            },
            { label: 'Started', value: <RelativeTime value={data.requestedAt} /> },
            {
              label: 'Went live',
              value: <RelativeTime value={data.acceptedAt} empty="Not live" />,
            },
            {
              label: 'Took',
              value: took ?? <span className="ui-text-subtle">—</span>,
            },
            { label: 'ID', value: <CopyId value={deployment} label="deployment ID" /> },
          ]}
        />
      </Section>
    </Page>
  );
}
