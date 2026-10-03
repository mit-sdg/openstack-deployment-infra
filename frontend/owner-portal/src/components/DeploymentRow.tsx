import { Link } from 'wouter';
import { short, time } from '../utils/presentation';
import { Status } from './Status';

export function DeploymentRow({
  id,
  deployment,
  active,
  href,
}: {
  id: string;
  deployment: {
    deploymentId: string;
    repositoryCommit: string | null;
    requestedAt: string | null;
    configurationRevision: number | null;
    status: string;
  };
  active?: string | null;
  href?: string;
}) {
  return (
    <div className="deployment-row">
      <div>
        <Link
          className="mono app-link"
          href={href ?? `/apps/${id}/deployments/${deployment.deploymentId}`}
        >
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
        href={href ?? `/apps/${id}/deployments/${deployment.deploymentId}`}
        aria-label={`View commit ${short(deployment.repositoryCommit)}`}
      >
        →
      </Link>
    </div>
  );
}
