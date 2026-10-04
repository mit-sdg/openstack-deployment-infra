import {
  Button,
  CodeBlock,
  CopyId,
  Hint,
  KeyValueList,
  PageSkeleton,
  RelativeTime,
  Section,
  SectionSkeleton,
  buttonClass,
} from '@openstack-platform/ui';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'wouter';
import { api, type Deployment } from '../api';
import { AppFrame } from '../components/AppFrame';
import { QueryError } from '../components/Feedback';
import { Repository } from '../components/Repository';
import { StartupRecordSection } from '../components/StartupRecord';
import { Status } from '../components/Status';
import { short } from '../utils/presentation';

/** "sha256:0123456789ab" from an image reference; the full value is copied. */
function imageLabel(reference: string) {
  const digest = reference.slice(reference.lastIndexOf('@') + 1);
  return digest.startsWith('sha256:') ? digest.slice(0, 19) : reference;
}

function Details({ deployment }: { deployment: Deployment }) {
  return (
    <KeyValueList
      columns={2}
      items={[
        {
          label: 'Commit',
          value: <CopyId value={deployment.repositoryCommit} label="commit SHA" length={9} />,
        },
        {
          label: 'Repository',
          value: <Repository url={deployment.sourceRepository} />,
        },
        { label: 'Started', value: <RelativeTime value={deployment.requestedAt} /> },
        { label: 'Went live', value: <RelativeTime value={deployment.acceptedAt} /> },
        {
          label: 'Runtime',
          value: deployment.configuration.build.runtime === 'node' ? 'Node.js' : 'Bun',
        },
        {
          label: 'Image',
          value: deployment.imageDigest ? (
            <CopyId
              value={deployment.imageDigest}
              label="image digest"
              display={imageLabel(deployment.imageDigest)}
            />
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
  // Only a failed deployment's new version was removed with a record.
  const startup = useQuery({
    queryKey: ['startup', id, deployment],
    queryFn: () => api.startupLog(id, deployment),
    enabled: attempt.data?.status === 'failed',
    staleTime: Infinity,
  });
  return (
    <AppFrame id={id} active="Deployments">
      {attempt.isPending ? (
        <PageSkeleton label="Loading deployment…">
          <SectionSkeleton title rows={3} />
          <SectionSkeleton title rows={4} />
        </PageSkeleton>
      ) : attempt.error ? (
        <QueryError query={attempt} what="this deployment" />
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
            <div>
              <Link
                href={`/apps/${id}/deploy?commit=${attempt.data.repositoryCommit}`}
                className={buttonClass()}
              >
                Deploy this commit again
              </Link>
            </div>
            <Hint>This uses your app’s current saved settings and environment variables.</Hint>
          </Section>
          {startup.data?.captured && (
            <StartupRecordSection
              record={startup.data}
              configuration={attempt.data.configuration}
            />
          )}
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
            {log.error && <QueryError query={log} what="the build output" />}
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
