import { Link } from 'wouter';
import { short, time } from '../utils/presentation';
import { Status } from './Status';

export function DeploymentRow({
  id,
  deployment,
  active,
}: {
  id: string;
  deployment: import('../api').Deployment;
  active?: string | null;
}) {
  return (
    <div className="deployment-row">
      <div>
        <Link className="mono app-link" href={`/apps/${id}/deployments/${deployment.deploymentId}`}>
          {short(deployment.repositoryCommit)}
        </Link>
        {deployment.deploymentId === active && (
          <span className="chip accepted-chip">Accepted version</span>
        )}
        <p className="cell-sub">
          {time(deployment.requestedAt)} · Settings revision {deployment.configurationRevision}
        </p>
      </div>
      <Status state={deployment.status} />
      <Link
        className="row-arrow"
        href={`/apps/${id}/deployments/${deployment.deploymentId}`}
        aria-label={`View commit ${short(deployment.repositoryCommit)}`}
      >
        →
      </Link>
    </div>
  );
}
