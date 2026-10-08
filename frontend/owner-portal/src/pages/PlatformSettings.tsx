import {
  Button,
  ErrorAlert,
  Field,
  Hint,
  InlineStatus,
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
import { SizeOptions } from '../components/SizeOptions';
import { sizingApi } from '../sizingApi';
import './app-pages.css';

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
      void client.invalidateQueries({ queryKey: ['default-builder-size'] }).then(() => {
        setSelected(null);
        setPendingKey(null);
      });
    }
  }, [intent.data?.state, client]);
  const change = useMutation({
    mutationFn: (key: string) =>
      sizingApi.setDefaultBuilder(selected!, settings.data!.flavor.name, key),
    onSuccess: (result) => setIntentId(result.intentId),
  });
  const busy =
    change.isPending ||
    settings.isFetching ||
    (!!intent.data && !['succeeded', 'failed'].includes(intent.data.state));
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
            <>
              {intent.data?.state === 'succeeded' && selected === null && (
                <InlineStatus>Default build machine saved.</InlineStatus>
              )}
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
                Save
              </Button>
            </>
          }
        >
          <Field label="Default build machine" id="default-builder-size" hint={builderExplanation}>
            <Select
              className="app-size-select"
              value={selected ?? settings.data.flavor.flavor_id}
              disabled={busy}
              onChange={(event) => {
                setSelected(event.target.value);
                setPendingKey(null);
              }}
            >
              <SizeOptions sizes={settings.data.sizes} current={settings.data.flavor} />
            </Select>
          </Field>
          <Hint>
            Changes apply to builds that start afterwards. Apps with their own build machine keep
            it.
          </Hint>
          <ErrorAlert error={change.error} />
          {intent.data && intent.data.state !== 'succeeded' && (
            <OperationList label="Default build machine change">
              <Operation intent={intent.data} showApp={false} />
            </OperationList>
          )}
        </Section>
      )}
    </Page>
  );
}
