import {
  Alert,
  Button,
  ErrorAlert,
  Field,
  Hint,
  Page,
  PageHeader,
  PageHeaderSkeleton,
  PageSkeleton,
  Section,
  SectionSkeleton,
  Select,
} from '@openstack-platform/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { builderExplanation } from '../components/BuilderSize';
import { QueryError } from '../components/Feedback';
import { Operation, OperationList } from '../components/Operation';
import { useIntentPolling } from '../hooks/useIntentPolling';
import { sizeLabel, sizingApi } from '../sizingApi';

export function PlatformSettingsPage() {
  const settings = useQuery({
    queryKey: ['default-builder-size'],
    queryFn: sizingApi.defaultBuilder,
  });
  const [selected, setSelected] = useState<string | null>(null);
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const [intentId, setIntentId] = useState<string | null>(null);
  const intent = useIntentPolling(intentId);
  const client = useQueryClient();
  useEffect(() => {
    if (intent.data?.state === 'succeeded') {
      void client.invalidateQueries({ queryKey: ['default-builder-size'] });
      setSelected(null);
      setPendingKey(null);
    }
  }, [intent.data?.state, client]);
  const change = useMutation({
    mutationFn: (key: string) =>
      sizingApi.setDefaultBuilder(selected!, settings.data!.flavor.name, key),
    onSuccess: (result) => setIntentId(result.intentId),
  });
  const busy =
    change.isPending || (!!intent.data && !['succeeded', 'failed'].includes(intent.data.state));
  if (settings.isPending)
    return (
      <PageSkeleton label="Loading platform settings…">
        <PageHeaderSkeleton />
        <SectionSkeleton title rows={2} />
      </PageSkeleton>
    );
  return (
    <Page width="narrow">
      <PageHeader title="Platform settings" />
      {settings.error && <QueryError query={settings} what="the platform settings" />}
      {settings.data && (
        <Section
          title="Builds"
          footer={
            <Button
              variant="primary"
              loading={change.isPending}
              disabled={selected === null || busy}
              onClick={() => {
                const key = pendingKey ?? crypto.randomUUID();
                setPendingKey(key);
                change.mutate(key);
              }}
            >
              Save default builder size
            </Button>
          }
        >
          <Field label="Default builder size" id="default-builder-size" hint={builderExplanation}>
            <Select
              value={selected ?? settings.data.flavor.flavor_id}
              disabled={busy}
              onChange={(event) => {
                setSelected(event.target.value);
                setPendingKey(null);
              }}
            >
              {!settings.data.sizes.some(
                (size) => size.flavor_id === settings.data.flavor.flavor_id,
              ) && (
                <option value={settings.data.flavor.flavor_id}>
                  {sizeLabel(settings.data.flavor)} (Current)
                </option>
              )}
              {settings.data.sizes.map((size) => (
                <option key={size.flavor_id} value={size.flavor_id}>
                  {sizeLabel(size)}
                  {size.flavor_id === settings.data.flavor.flavor_id ? ' (Current)' : ''}
                </option>
              ))}
            </Select>
          </Field>
          <Hint>
            Applies to builds that start afterwards for apps using the platform default.
            App-specific builder sizes stay the same.
          </Hint>
          <ErrorAlert error={change.error} />
          {intent.data && (
            <OperationList label="Default builder size change">
              <Operation intent={intent.data} showApp={false} />
            </OperationList>
          )}
          {intent.data?.state === 'succeeded' && (
            <Alert tone="success">Default builder size saved.</Alert>
          )}
        </Section>
      )}
    </Page>
  );
}
