import {
  Button,
  CodeBlock,
  ErrorAlert,
  Hint,
  Icon,
  KeyValueList,
  LoadingRows,
  Section,
  backLinkClass,
} from '@openstack-platform/ui';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'wouter';
import { api, type Deployment } from '../api';
import { AppFrame } from '../components/AppFrame';
import { BoundaryText } from '../components/BoundaryText';
import { Status } from '../components/Status';
import { relativeTime, short } from '../utils/presentation';

function When({ value }: { value: string | null }) {
  return value ? (
    <time dateTime={value} title={new Date(value).toLocaleString()}>
      {relativeTime(value)}
    </time>
  ) : (
    <span className="ui-text-subtle">—</span>
  );
}

function Details({ deployment }: { deployment: Deployment }) {
  return (
    <KeyValueList
      columns={2}
      items={[
        {
          label: 'Commit',
          value: <span className="ui-mono ui-break">{deployment.repositoryCommit}</span>,
        },
        {
          label: 'Repository',
          value: <BoundaryText text={deployment.sourceRepository} />,
        },
        { label: 'Started', value: <When value={deployment.requestedAt} /> },
        { label: 'Went live', value: <When value={deployment.acceptedAt} /> },
        {
          label: 'Runtime',
          value: deployment.configuration.build.runtime === 'node' ? 'Node.js' : 'Bun',
        },
        {
          label: 'Image',
          value: deployment.imageDigest ? (
            <span className="ui-mono ui-truncate" title={deployment.imageDigest}>
              {deployment.imageDigest}
            </span>
          ) : (
            <span className="ui-text-subtle">Not built yet</span>
          ),
        },
      ]}
    />
  );
}

export function DeploymentPage({ id, deployment }: { id: string; deployment: string }) {
  const [paused, setPaused] = useState(false);
  const attempt = useQuery({
    queryKey: ['deployment', id, deployment],
    queryFn: () => api.deployment(id, deployment),
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 1500 : false),
  });
  const log = useQuery({
    queryKey: ['log', id, deployment],
    queryFn: () => api.log(id, deployment),
    enabled: !!attempt.data,
    refetchInterval: !paused && attempt.data?.status === 'running' ? 1500 : false,
  });
  const running = attempt.data?.status === 'running';
  return (
    <AppFrame id={id} active="Deployments">
      <div>
        <Link href={`/apps/${id}/deployments`} className={backLinkClass}>
          <Icon name="arrow-left" />
          All deployments
        </Link>
      </div>
      {attempt.isPending ? (
        <Section title="Deployment">
          <LoadingRows />
        </Section>
      ) : attempt.error ? (
        <ErrorAlert error={attempt.error} />
      ) : (
        <>
          <Section
            title={
              <>
                Deployment <span className="ui-mono">{short(attempt.data.repositoryCommit)}</span>
              </>
            }
            actions={<Status state={attempt.data.status} />}
          >
            <Details deployment={attempt.data} />
          </Section>
          <Section
            title="Build output"
            actions={
              <>
                {running && (
                  <Button size="sm" variant="ghost" onClick={() => setPaused(!paused)}>
                    {paused ? 'Resume updates' : 'Pause updates'}
                  </Button>
                )}
                <Button size="sm" variant="ghost" onClick={() => log.refetch()}>
                  Refresh
                </Button>
              </>
            }
          >
            <ErrorAlert error={log.error} focus={false} />
            <CodeBlock label="Build log" variant="log">
              {log.data?.text.replace(/\x1b\[[0-9;]*[A-Za-z]/g, '') ||
                'Build output will appear here.'}
            </CodeBlock>
            <Hint>
              {log.data?.truncated ? 'Showing the last 200 lines. ' : ''}
              Build output can include anything your build prints, so share it with care.
            </Hint>
          </Section>
        </>
      )}
    </AppFrame>
  );
}
