import { Button, List, ListItem, RelativeTime } from '@openstack-platform/ui';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { Link } from 'wouter';
import { api, type Intent } from '../api';
import { activityTitle, humanPhase, short } from '../utils/presentation';
import { ErrorNotice } from './Feedback';
import { Status } from './Status';

const finished = ['succeeded', 'failed', 'blocked'];

/** One activity row. Put rows inside <OperationList>. */
export function Operation({ intent, showApp = true }: { intent: Intent; showApp?: boolean }) {
  const client = useQueryClient();
  const resume = useMutation({
    mutationFn: () => api.resume(intent.intentId),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ['intents'] });
      client.invalidateQueries({ queryKey: ['intent', intent.intentId] });
    },
  });
  const title = activityTitle(intent.kind, intent.state);
  const progress =
    intent.state === 'unknown'
      ? intent.requiresResubmit
        ? 'Enter the value again to finish this change'
        : 'Reconnecting…'
      : intent.operation?.phase && !finished.includes(intent.state)
        ? humanPhase(intent.operation.phase)
        : null;
  return (
    <ListItem
      title={
        intent.operationId && intent.kind === 'deploy' ? (
          <Link
            href={`/apps/${intent.appId}/deployments/${intent.operationId}`}
            className="ui-link ui-link--plain"
          >
            {title}
          </Link>
        ) : (
          title
        )
      }
      meta={
        <>
          {showApp && (
            <Link href={`/apps/${intent.appId}`} className="ui-link">
              {intent.appSlug ?? 'App'}
            </Link>
          )}
          {intent.names?.[0] && <code>{intent.names[0]}</code>}
          {intent.commit && <code>{short(intent.commit)}</code>}
          {progress && <span>{progress}</span>}
          <RelativeTime value={intent.createdAt} />
        </>
      }
      trailing={
        <>
          {intent.state === 'blocked' && !intent.requiresResubmit && (
            <Button size="sm" onClick={() => resume.mutate()} loading={resume.isPending}>
              Resume
            </Button>
          )}
          {/* Feeds show only states that need attention. */}
          <Status state={intent.state} quiet="hidden" />
        </>
      }
    >
      {(intent.safeError || intent.controllerErrorCode) && (
        <p className="ui-text-danger ui-text-sm">
          {intent.safeError}
          {intent.safeError && intent.controllerErrorCode && ' '}
          {intent.controllerErrorCode && (
            <span className="ui-text-subtle">
              Error code <code>{intent.controllerErrorCode}</code>
            </span>
          )}
          {intent.kind === 'deploy' && intent.state === 'failed' && intent.operationId && (
            <>
              {' '}
              <Link
                href={`/apps/${intent.appId}/deployments/${intent.operationId}`}
                className="ui-link"
              >
                See why it stopped
              </Link>
            </>
          )}
        </p>
      )}
      <ErrorNotice error={resume.error} />
    </ListItem>
  );
}

export function OperationList({ label, children }: { label: string; children: ReactNode }) {
  return (
    <List label={label} density="compact">
      {children}
    </List>
  );
}
