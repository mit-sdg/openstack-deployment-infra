import { useQuery } from '@tanstack/react-query';
import { api } from '../api';
import { OperationStatus } from './Operation';
import { QueryError } from './Feedback';
import { Status } from './Status';
/** Join controller deployment records with the durable change; never infer Resume from deployment status alone. */
export function DeploymentStatus({
  id,
  deployment,
  state,
}: {
  id: string;
  deployment: string;
  state: string;
}) {
  const attention = useQuery({
    queryKey: ['attention', id],
    queryFn: () => api.attention(id),
    refetchInterval: 5000,
  });
  const intent = attention.data?.find((item) => item.operationId === deployment);
  if (attention.error)
    return (
      <>
        <Status state={state} />
        <QueryError query={attention} what="the unfinished change" />
      </>
    );
  return intent ? <OperationStatus intent={intent} /> : <Status state={state} />;
}
