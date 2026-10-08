import { Section } from '@openstack-platform/ui';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api';
import { QueryError } from './Feedback';
import { Operation, OperationList } from './Operation';

/** Attention is queried separately so an older blocked change never ages out. */
export function AttentionActivity({ id }: { id: string }) {
  const activity = useQuery({
    queryKey: ['attention', id],
    queryFn: () => api.attention(id),
    refetchInterval: 5000,
  });
  if (activity.error) return <QueryError query={activity} what="activity that needs attention" />;
  if (!activity.data?.length) return null;
  return (
    <Section title="Needs attention" flush>
      <OperationList label="Activity that needs attention">
        {activity.data.map((intent) => (
          <Operation key={intent.intentId} intent={intent} showApp={false} showActor />
        ))}
      </OperationList>
    </Section>
  );
}
