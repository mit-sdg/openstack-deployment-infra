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
export function Operation({
  intent,
  showApp = true,
  managed = false,
  showActor = false,
}: {
  intent: Intent;
  showApp?: boolean;
  managed?: boolean;
  showActor?: boolean;
}) {
  const client = useQueryClient();
  const resume = useMutation({
    mutationFn: () => api.resume(intent.intentId),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ['intents'] });
      client.invalidateQueries({ queryKey: ['activity', intent.appId] });
      client.invalidateQueries({ queryKey: ['intent', intent.intentId] });
      client.invalidateQueries({ queryKey: ['attention', intent.appId] });
      client.invalidateQueries({ queryKey: ['app', intent.appId] });
      client.invalidateQueries({ queryKey: ['history', intent.appId] });
      client.invalidateQueries({ queryKey: ['admin', 'app', intent.appId] });
      client.invalidateQueries({ queryKey: ['staff'] });
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
        !managed && intent.operationId && intent.kind === 'deploy' ? (
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
          {intent.actor && (showActor || !intent.actor.you) && intent.actor.displayName && (
            <span>{intent.actor.displayName}</span>
          )}
          <RelativeTime value={intent.createdAt} />
        </>
      }
      trailing={
        <>
          {intent.state === 'blocked' && intent.canResume !== false && !intent.requiresResubmit && (
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
          {!managed &&
            intent.kind === 'deploy' &&
            intent.state === 'failed' &&
            intent.operationId && (
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
      {intent.state === 'blocked' && ['env_set', 'env_delete'].includes(intent.kind) && (
        <p className="ui-text-sm">
          The person who started this environment edit must enter the value again in Environment
          variables.
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
