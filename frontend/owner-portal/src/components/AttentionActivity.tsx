import { Alert, Section } from '@openstack-platform/ui';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api';
import { adminAppsApi } from '../adminAppsApi';
import { QueryError } from './Feedback';
import { Operation, OperationList } from './Operation';

/** Attention is queried separately so an older blocked change never ages out. */
export function AttentionActivity({ id, managed = false }: { id: string; managed?: boolean }) {
  const activity = useQuery({
    queryKey: ['attention', id, managed],
    queryFn: () => (managed ? adminAppsApi.attention(id) : api.attention(id)),
    refetchInterval: 5000,
  });
  if (activity.error) return <QueryError query={activity} what="activity that needs attention" />;
  if (!activity.data?.length) return null;
  return (
    <Section title="Activity" flush>
      <Alert tone="warning">
        A previous change hasn’t finished. Finish it here before making another change.
      </Alert>
      <OperationList label="Activity that needs attention">
        {activity.data.map((intent) => (
          <Operation
            key={intent.intentId}
            intent={intent}
            showApp={false}
            managed={managed}
            showActor
          />
        ))}
      </OperationList>
    </Section>
  );
}
