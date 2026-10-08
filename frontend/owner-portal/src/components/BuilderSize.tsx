import { Alert, Button, ErrorAlert, Field, Hint, Select, Skeleton } from '@openstack-platform/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { useIntentPolling } from '../hooks/useIntentPolling';
import { sizingApi, sizeLabel, type Flavor } from '../sizingApi';
import { QueryError } from './Feedback';
import { Operation, OperationList } from './Operation';

export const builderExplanation =
  'Each deploy builds on a temporary machine of this size. Bigger builds faster and fits heavier builds, such as Next.js; smaller lets more builds run at once. If a build runs out of memory, pick a bigger builder size.';

export function BuilderSizeControl({ id, sizes }: { id: string; sizes: Flavor[] }) {
  const current = useQuery({
    queryKey: ['builder-size', id],
    queryFn: () => sizingApi.builder(id),
  });
  const [selected, setSelected] = useState<string | null>(null);
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const [intentId, setIntentId] = useState<string | null>(null);
  const intent = useIntentPolling(intentId);
  const client = useQueryClient();
  useEffect(() => {
    if (intent.data?.state === 'succeeded') {
      void client.invalidateQueries({ queryKey: ['builder-size', id] });
      setSelected(null);
      setPendingKey(null);
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
    change.isPending || (!!intent.data && !['succeeded', 'failed'].includes(intent.data.state));
  return (
    <>
      {current.isPending ? (
        <Skeleton variant="block" />
      ) : current.error ? (
        <QueryError query={current} what="the builder size" />
      ) : (
        <>
          <Field label="Builder size" id="builder-size" hint={builderExplanation}>
            <Select
              value={value}
              disabled={busy}
              onChange={(event) => {
                setSelected(event.target.value);
                setPendingKey(null);
              }}
            >
              <option value="">
                Platform default ({current.data.defaultFlavor.vcpus} vCPU ·{' '}
                {current.data.defaultFlavor.ram_mib / 1024} GB)
              </option>
              {!sizes.some((size) => size.flavor_id === current.data.flavor.flavor_id) &&
                !current.data.useDefault && (
                  <option value={current.data.flavor.flavor_id}>
                    {sizeLabel(current.data.flavor)} (Current)
                  </option>
                )}
              {sizes
                .filter((size) => size.ram_mib >= 1024)
                .map((size) => (
                  <option key={size.flavor_id} value={size.flavor_id}>
                    {sizeLabel(size)}
                    {!current.data.useDefault && size.flavor_id === current.data.flavor.flavor_id
                      ? ' (Current)'
                      : ''}
                  </option>
                ))}
            </Select>
          </Field>
          <Hint>
            Current builder: {sizeLabel(current.data.flavor)}
            {current.data.useDefault ? ' · Platform default' : ' · App-specific'}
          </Hint>
          <Hint>
            Saving applies to builds that start afterwards. It keeps the worker and needs no outage.
          </Hint>
          <ErrorAlert error={change.error} />
          <Button
            disabled={selected === null || busy}
            loading={change.isPending}
            onClick={() => {
              const key = pendingKey ?? crypto.randomUUID();
              setPendingKey(key);
              change.mutate(key);
            }}
          >
            Save builder size
          </Button>
        </>
      )}
      {intent.data && (
        <OperationList label="Builder size change">
          <Operation intent={intent.data} showApp={false} />
        </OperationList>
      )}
      {intent.data?.state === 'succeeded' && <Alert tone="success">Builder size saved.</Alert>}
    </>
  );
}
