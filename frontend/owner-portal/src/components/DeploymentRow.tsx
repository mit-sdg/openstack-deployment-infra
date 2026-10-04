import { ListItem, RelativeTime } from '@openstack-platform/ui';
import { Link } from 'wouter';
import { short } from '../utils/presentation';
import { Status } from './Status';

/** One deployment as a list row. Put rows inside a shared <List>. */
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
    <ListItem
      title={
        <Link
          className="ui-link ui-link--plain ui-mono"
          href={href ?? `/apps/${id}/deployments/${deployment.deploymentId}`}
        >
          {short(deployment.repositoryCommit)}
        </Link>
      }
      meta={
        deployment.requestedAt && (
          <span>
            Started <RelativeTime value={deployment.requestedAt} />
          </span>
        )
      }
      trailing={
        // The live deployment reads "Live"; it succeeded by definition.
        <Status state={deployment.deploymentId === active ? 'live' : deployment.status} />
      }
    />
  );
}
