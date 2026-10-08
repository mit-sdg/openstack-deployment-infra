import { Cluster, Hint, KeyValueList, Section, Skeleton, Stack } from '@openstack-platform/ui';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'wouter';
import type { AppRecord } from '../api';
import { sizingApi, workerSizeLabel } from '../sizingApi';
import { QueryError } from './Feedback';

export function Machines({ app }: { app: AppRecord }) {
  const id = app.applicationId;
  const builder = useQuery({
    queryKey: ['builder-size', id],
    queryFn: () => sizingApi.builder(id),
  });
  const size = app.sizing;
  const capacity =
    size?.vcpus && size.ram_mib
      ? { name: size.workerFlavor, vcpus: size.vcpus, ram_mib: size.ram_mib }
      : null;
  return (
    <Section title="Machines" aria-label="Machines">
      <KeyValueList
        items={[
          {
            label: 'Worker',
            stacked: true,
            value: (
              <Stack gap={2}>
                <Cluster gap={3}>
                  <span>
                    {capacity
                      ? workerSizeLabel(capacity)
                      : size
                        ? `${size.workerFlavor} · ${(size.memoryMiB / 1024).toFixed(1)} GB memory for the app · ${(size.cpuMHz / 1000).toFixed(1)} GHz CPU in total`
                        : 'Worker details unavailable'}
                  </span>
                  <Link className="ui-link ui-text-sm" href={`/apps/${id}/deploy#worker-size`}>
                    Change size
                  </Link>
                </Cluster>
                {capacity && size && (
                  <Hint>
                    The app can use up to {(size.memoryMiB / 1024).toFixed(1)} GB of memory and{' '}
                    {capacity.vcpus === 1 ? '1 vCPU' : `all ${capacity.vcpus} vCPUs`} (about{' '}
                    {(size.cpuMHz / capacity.vcpus / 1000).toFixed(1)} GHz each).
                  </Hint>
                )}
              </Stack>
            ),
          },
          {
            label: 'Build machine',
            stacked: true,
            value: (
              <Cluster gap={3}>
                {builder.isPending ? (
                  <Skeleton />
                ) : builder.error ? (
                  <QueryError query={builder} what="the build machine" />
                ) : (
                  <span>
                    {workerSizeLabel(builder.data.flavor)} ·{' '}
                    {builder.data.useDefault ? 'platform default' : 'set for this app'}
                  </span>
                )}
                <Link
                  className="ui-link ui-text-sm"
                  href={`/apps/${id}/configuration#builder-size`}
                >
                  Change
                </Link>
              </Cluster>
            ),
          },
        ]}
      />
    </Section>
  );
}
