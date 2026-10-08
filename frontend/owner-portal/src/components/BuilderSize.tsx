import {
  Button,
  Cluster,
  ErrorAlert,
  Field,
  Hint,
  InlineStatus,
  Section,
  Select,
  Skeleton,
} from '@openstack-platform/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { useIntentPolling } from '../hooks/useIntentPolling';
import { sizingApi } from '../sizingApi';
import { QueryError } from './Feedback';
import { Operation, OperationList } from './Operation';
import { SizeOptions } from './SizeOptions';

export const builderExplanation =
  'Each deploy builds the app on a temporary machine of this size. A bigger machine builds faster and handles heavy builds such as Next.js; a smaller one lets more builds run at once. If a build runs out of memory, choose a bigger machine.';

export function BuilderSizeControl({ id }: { id: string }) {
  const current = useQuery({
    queryKey: ['builder-size', id],
    queryFn: () => sizingApi.builder(id),
  });
  const sizes = useQuery({ queryKey: ['sizes', id], queryFn: () => sizingApi.sizes(id) });
  const [selected, setSelected] = useState<string | null>(null);
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const [intentId, setIntentId] = useState<string | null>(null);
  const intent = useIntentPolling(intentId);
  const client = useQueryClient();
  useEffect(() => {
    if (intent.data?.state === 'succeeded') {
      void client.invalidateQueries({ queryKey: ['builder-size', id] }).then(() => {
        setSelected(null);
        setPendingKey(null);
      });
    }
  }, [intent.data?.state, client, id]);
  const change = useMutation({
    mutationFn: (key: string) =>
      sizingApi.setBuilder(
        id,
        selected === '' ? null : selected!,
        current.data!.useDefault ? null : current.data!.flavor.name,
        key,
      ),
    onSuccess: (result) => setIntentId(result.intentId),
  });
  const value =
    selected ?? (current.data?.useDefault ? '' : (current.data?.flavor.flavor_id ?? ''));
  const busy =
    change.isPending ||
    current.isFetching ||
    (!!intent.data && !['succeeded', 'failed'].includes(intent.data.state));
  const persisted = current.data?.useDefault ? '' : current.data?.flavor.flavor_id;
  const changed = selected !== null && value !== persisted;
  function choose(value: string) {
    setSelected(value);
    setPendingKey(null);
  }
  return (
    <Section
      title="Build machine"
      aria-label="Build machine settings"
      footer={
        current.data && (
          <>
            {intent.data?.state === 'succeeded' && selected === null && (
              <InlineStatus>Build machine saved.</InlineStatus>
            )}
            <Button
              disabled={!changed || busy}
              loading={change.isPending}
              onClick={() => {
                const key = pendingKey ?? crypto.randomUUID();
                setPendingKey(key);
                change.mutate(key);
              }}
            >
              Save
            </Button>
          </>
        )
      }
    >
      {current.isPending ? (
        <Skeleton variant="block" />
      ) : current.error ? (
        <QueryError query={current} what="the build machine" />
      ) : (
        <>
          {sizes.error && <QueryError query={sizes} what="the available sizes" />}
          <Field label="Build machine" id="builder-size" hint={builderExplanation}>
            <Select
              className="app-size-select"
              value={value}
              disabled={busy}
              onChange={(event) => choose(event.target.value)}
            >
              <option value="">
                Platform default ({current.data.defaultFlavor.vcpus} vCPU ·{' '}
                {current.data.defaultFlavor.ram_mib / 1024} GB)
              </option>
              <SizeOptions
                sizes={(sizes.data ?? []).filter((size) => size.ram_mib >= 1024)}
                current={current.data.useDefault ? undefined : current.data.flavor}
              />
            </Select>
          </Field>
          <Cluster gap={2}>
            <span className="ui-text-muted ui-text-sm">
              {value === '' ? 'Uses the platform default' : 'Set for this app'}
            </span>
            {value !== '' && (
              <Button variant="ghost" size="sm" disabled={busy} onClick={() => choose('')}>
                Use platform default
              </Button>
            )}
          </Cluster>
          <Hint>Changes apply to builds that start afterwards.</Hint>
          <ErrorAlert error={change.error} />
        </>
      )}
      {intent.data && intent.data.state !== 'succeeded' && (
        <OperationList label="Build machine change">
          <Operation intent={intent.data} showApp={false} />
        </OperationList>
      )}
    </Section>
  );
}
