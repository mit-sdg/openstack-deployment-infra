import {
  Button,
  Cluster,
  ErrorAlert,
  Field,
  Grid,
  Hint,
  InlineStatus,
  Input,
  RelativeTime,
  Select,
  Stack,
} from '@openstack-platform/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { api, resourceApi, type Intent, type StorageQuotas, type StorageResource } from '../api';
import { Status } from './Status';

const numbers = new Intl.NumberFormat('en-US');
const bytes = (value: number) =>
  value < 1024
    ? `${numbers.format(value)} B`
    : value < 1024 ** 2
      ? `${numbers.format(Math.round((value / 1024) * 10) / 10)} KB`
      : value >= 1024 ** 3
        ? `${numbers.format(Math.round((value / 1024 ** 3) * 10) / 10)} GB`
        : `${numbers.format(Math.round((value / 1024 ** 2) * 10) / 10)} MB`;

export function StorageUsage({ resource }: { resource: StorageResource }) {
  const { usage, quotas, writeBlock } = resource;
  const size = quotas.s3Bytes ?? quotas.sizeBytes;
  return (
    <Stack gap={2} className="app-storage-usage">
      {size !== undefined && (
        <div>
          <div>
            {resource.type === 'postgres' ? 'Size target' : 'Size limit'}:{' '}
            {usage.usedBytes === null ? bytes(size) : `${bytes(usage.usedBytes)} of ${bytes(size)}`}
          </div>
          {usage.usedBytes !== null && (
            <progress
              className="app-storage-meter"
              aria-label={`${resource.label} size usage`}
              value={Math.min(usage.usedBytes, size)}
              max={size}
            />
          )}
          {resource.type === 'postgres' && (
            <Hint>The size target is for reporting; it does not pause writes.</Hint>
          )}
        </div>
      )}
      {quotas.s3Objects !== undefined && (
        <div>
          Objects:{' '}
          {usage.objectCount === null
            ? `Limit ${numbers.format(quotas.s3Objects)}`
            : `${numbers.format(usage.objectCount)} of ${numbers.format(quotas.s3Objects)}`}
        </div>
      )}
      {quotas.connections !== undefined && (
        <div>
          Connections:{' '}
          {usage.currentConnections === null
            ? `Limit ${numbers.format(quotas.connections)}`
            : `${numbers.format(usage.currentConnections)} of ${numbers.format(quotas.connections)}`}
        </div>
      )}
      {quotas.memoryBytes !== undefined && (
        <div>
          {resource.isolation === 'shared' ? 'Planned memory limit' : 'Memory limit'}:{' '}
          {usage.instanceMemoryBytes === null
            ? bytes(quotas.memoryBytes)
            : `${bytes(usage.instanceMemoryBytes)} of ${bytes(quotas.memoryBytes)}`}
        </div>
      )}
      {quotas.cpuMillicores !== undefined && (
        <div>
          {resource.isolation === 'shared' ? 'Planned CPU limit' : 'CPU limit'}:{' '}
          {numbers.format(quotas.cpuMillicores / 1000)} cores
        </div>
      )}
      {usage.cpuTimeMilliseconds !== null && (
        <div>
          CPU time: {numbers.format(Math.round(usage.cpuTimeMilliseconds / 100) / 10)} seconds total
        </div>
      )}
      {resource.hardQuotaBytes !== null && (
        <Hint>
          Disk allowance: {bytes(resource.hardQuotaBytes)}, including database files and overhead.
        </Hint>
      )}
      {resource.type !== 's3' && resource.isolation === 'shared' && (
        <Hint>
          This database is awaiting its move to a separate instance. Ask an admin before changing
          memory or CPU limits.
        </Hint>
      )}
      <div className="ui-text-subtle ui-text-sm">
        {usage.measuredAt ? (
          <>
            Updated <RelativeTime value={usage.measuredAt} />
            {usage.stale && ' · Usage may be out of date'}
          </>
        ) : (
          'Usage not measured yet'
        )}
      </div>
      {writeBlock.blocked && (
        <InlineStatus tone="warning">
          Writes are paused because this database is over its size limit. You can still read and
          delete data. Delete enough data to leave at least 5% of your limit free; writes resume
          automatically within a few minutes. Staff can arrange a higher limit.
        </InlineStatus>
      )}
    </Stack>
  );
}

export function StorageLimitsControl({
  id,
  resource,
  service,
  disabled,
}: {
  id: string;
  resource: StorageResource;
  service: ReturnType<typeof resourceApi>;
  disabled?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  return editing ? (
    <LimitsForm
      key={resource.resourceId}
      id={id}
      resource={resource}
      service={service}
      cancel={() => setEditing(false)}
    />
  ) : (
    <Cluster>
      <Button
        size="sm"
        variant="ghost"
        disabled={disabled}
        aria-label={`Edit ${resource.label} limits`}
        onClick={() => setEditing(true)}
      >
        Edit limits
      </Button>
    </Cluster>
  );
}

function LimitsForm({
  id,
  resource,
  service,
  cancel,
}: {
  id: string;
  resource: StorageResource;
  service: ReturnType<typeof resourceApi>;
  cancel: () => void;
}) {
  const client = useQueryClient();
  const [expected] = useState(() => ({ ...resource.quotas }));
  const sizeKey = resource.type === 's3' ? 's3Bytes' : 'sizeBytes';
  const initialSize = expected[sizeKey]!;
  const [unit, setUnit] = useState(initialSize % 1024 ** 3 === 0 ? 'GB' : 'MB');
  const [size, setSize] = useState(
    String(initialSize / (initialSize % 1024 ** 3 === 0 ? 1024 ** 3 : 1024 ** 2)),
  );
  const countKey = resource.type === 's3' ? 's3Objects' : 'connections';
  const [count, setCount] = useState(String(expected[countKey] ?? ''));
  const [memory, setMemory] = useState(String((expected.memoryBytes ?? 536870912) / 1048576));
  const [cpu, setCpu] = useState(String((expected.cpuMillicores ?? 500) / 1000));
  const [validation, setValidation] = useState<{
    size?: string;
    count?: string;
    memory?: string;
    cpu?: string;
  } | null>(null);
  const [intent, setIntent] = useState<Intent | null>(null);
  const pending = useRef<{ body: string; key: string } | null>(null);
  const mutation = useMutation({
    mutationFn: (quotas: StorageQuotas) => {
      const body = JSON.stringify({ quotas, expected });
      if (pending.current?.body !== body) pending.current = { body, key: crypto.randomUUID() };
      return service.setStorageLimits(
        id,
        resource.resourceId,
        quotas,
        expected,
        pending.current.key,
      );
    },
    onSuccess: (result) => {
      setIntent(result);
      void client.invalidateQueries({ queryKey: ['storage', id] });
      void client.invalidateQueries({ queryKey: ['intents'] });
    },
  });
  const observation = useQuery({
    queryKey: ['intent', intent?.intentId],
    queryFn: async () => {
      const result = await api.intent(intent!.intentId);
      if (['succeeded', 'failed'].includes(result.state)) {
        pending.current = null;
        void client.invalidateQueries({ queryKey: ['storage', id] });
      }
      return result;
    },
    enabled: !!intent && !['succeeded', 'failed'].includes(intent.state),
    refetchInterval: (query) =>
      ['succeeded', 'failed', 'blocked'].includes(query.state.data?.state ?? '') ? false : 1500,
  });
  const result = observation.data ?? intent;
  const busy = mutation.isPending || (!!result && !['succeeded', 'failed'].includes(result.state));
  const prefix = `limits-${resource.resourceId}`;
  return (
    <form
      className="app-storage-limits"
      aria-label={`${resource.label} limits`}
      onSubmit={(event) => {
        event.preventDefault();
        const rawBytes = Number(size) * (unit === 'GB' ? 1024 ** 3 : 1024 ** 2);
        const value = Math.round(rawBytes);
        const amount = Number(count);
        if (
          !size.trim() ||
          !Number.isSafeInteger(value) ||
          rawBytes < 1048576 ||
          rawBytes > 549755813888
        ) {
          setValidation({ size: 'Choose a size from 1 MB to 500 GB.' });
          return;
        }
        if (
          !count.trim() ||
          !Number.isSafeInteger(amount) ||
          amount < 1 ||
          amount > (resource.type === 's3' ? 100000000 : 100)
        ) {
          setValidation({
            count:
              resource.type === 's3'
                ? 'Choose 1 to 100,000,000 objects.'
                : 'Choose 1 to 100 connections.',
          });
          return;
        }
        const memoryBytes = Number(memory) * 1048576;
        const cpuCores = Number(cpu);
        const cpuMillicores = Math.round(cpuCores * 1000);
        if (
          resource.type !== 's3' &&
          (!memory.trim() ||
            !Number.isSafeInteger(memoryBytes) ||
            memoryBytes % 1048576 ||
            memoryBytes < 536870912 ||
            memoryBytes > 8589934592)
        ) {
          setValidation({ memory: 'Choose 512 to 8,192 MB in whole MB.' });
          return;
        }
        if (
          resource.type !== 's3' &&
          (!cpu.trim() || !Number.isSafeInteger(cpuMillicores) || cpuCores < 0.1 || cpuCores > 4)
        ) {
          setValidation({ cpu: 'Choose 0.1 to 4 CPU cores, in increments of 0.001.' });
          return;
        }
        setValidation(null);
        mutation.mutate({
          [sizeKey]: value,
          [countKey]: amount,
          ...(resource.type === 's3' ? {} : { memoryBytes, cpuMillicores }),
        });
      }}
    >
      <Stack gap={3}>
        <Grid columns={2}>
          <Field
            id={`${prefix}-size`}
            label={resource.type === 'postgres' ? 'Size target' : 'Size limit'}
            error={validation?.size}
          >
            <Input
              type="number"
              min={unit === 'GB' ? 1 / 1024 : 1}
              max={unit === 'GB' ? 500 : 512000}
              step="any"
              value={size}
              disabled={busy}
              onChange={(event) => setSize(event.target.value)}
            />
          </Field>
          <Field id={`${prefix}-unit`} label="Size unit">
            <Select
              value={unit}
              disabled={busy}
              onChange={(event) => {
                const next = event.target.value;
                setSize(String(Number(size) * (next === 'GB' ? 1 / 1024 : 1024)));
                setUnit(next);
              }}
            >
              <option value="MB">MB</option>
              <option value="GB">GB</option>
            </Select>
          </Field>
          <Field
            id={`${prefix}-count`}
            label={resource.type === 's3' ? 'Object limit' : 'Connection limit'}
            error={validation?.count}
          >
            <Input
              type="number"
              min={1}
              max={resource.type === 's3' ? 100000000 : 100}
              step={1}
              disabled={busy}
              value={count}
              onChange={(event) => setCount(event.target.value)}
            />
          </Field>
          {resource.type !== 's3' && (
            <>
              <Field id={`${prefix}-memory`} label="Memory limit (MB)" error={validation?.memory}>
                <Input
                  type="number"
                  min={512}
                  max={8192}
                  step={1}
                  value={memory}
                  disabled={busy || resource.isolation === 'shared'}
                  onChange={(event) => setMemory(event.target.value)}
                />
              </Field>
              <Field id={`${prefix}-cpu`} label="CPU limit (cores)" error={validation?.cpu}>
                <Input
                  type="number"
                  min={0.1}
                  max={4}
                  step={0.001}
                  value={cpu}
                  disabled={busy || resource.isolation === 'shared'}
                  onChange={(event) => setCpu(event.target.value)}
                />
              </Field>
            </>
          )}
        </Grid>
        <Hint>
          1 GB = 1,024 MB. Allowed size: 1 MB to 500 GB.{' '}
          {resource.type === 'postgres' && 'The size target is not enforced.'}
        </Hint>
        {(!!mutation.error || !!observation.error) && (
          <ErrorAlert error={mutation.error ?? observation.error} />
        )}
        {result && (
          <>
            <Status state={result.state} />
            {result.safeError && <InlineStatus tone="danger">{result.safeError}</InlineStatus>}
            {result.state === 'succeeded' && (
              <InlineStatus tone="success">Limits saved.</InlineStatus>
            )}
          </>
        )}
        <Cluster>
          <Button size="sm" variant="ghost" onClick={cancel}>
            {busy ? 'Close' : 'Cancel'}
          </Button>
          <Button
            size="sm"
            type="submit"
            loading={busy}
            disabled={busy || result?.state === 'succeeded'}
          >
            Save limits
          </Button>
        </Cluster>
      </Stack>
    </form>
  );
}
