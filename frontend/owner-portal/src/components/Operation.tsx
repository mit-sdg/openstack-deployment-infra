import { Button, ErrorAlert, List, ListItem, RelativeTime } from '@openstack-platform/ui';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { Link } from 'wouter';
import { api, type Intent } from '../api';
import { activityTitle, humanPhase, short } from '../utils/presentation';
import { Status } from './Status';

const finished = ['succeeded', 'failed', 'blocked', 'unknown'];
export const needsAttention = (intent: Pick<Intent, 'state'>) =>
  ['blocked', 'unknown'].includes(intent.state);

/** One activity row. Put rows inside <OperationList>. */
export function Operation({
  intent,
  showApp = true,
  showActor = false,
}: {
  intent: Intent;
  showApp?: boolean;
  showActor?: boolean;
}) {
  const title = activityTitle(intent.kind, intent.state);
  const environmentEdit = needsAttention(intent) && ['env_set', 'env_delete'].includes(intent.kind);
  const message = environmentEdit
    ? 'The person who started this environment edit must enter the value again in Settings.'
    : intent.safeError;
  const progress =
    intent.operation?.phase && !finished.includes(intent.state)
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
          {showApp && intent.appId && (
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
          <OperationStatus intent={intent} quiet="hidden" />
        </>
      }
    >
      {(message || intent.controllerErrorCode) && (
        <p className="ui-text-danger ui-text-sm">
          {message}
          {message && intent.controllerErrorCode && ' '}
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

/** One status and Resume control, including the write-only environment exception. */
export function OperationStatus({
  intent,
  quiet = 'text',
}: {
  intent: Intent;
  quiet?: 'text' | 'hidden';
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
      client.invalidateQueries({ queryKey: ['deployment', intent.appId] });
      client.invalidateQueries({ queryKey: ['all-apps'] });
      client.invalidateQueries({ queryKey: ['class'] });
      client.invalidateQueries({ queryKey: ['builder-size', intent.appId] });
      client.invalidateQueries({ queryKey: ['default-builder-size'] });
    },
  });

  return (
    <span className="ui-cluster ui-gap-2">
      <Status state={intent.state} quiet={quiet} />
      {needsAttention(intent) && intent.canResume !== false && !intent.requiresResubmit && (
        <Button size="sm" onClick={() => resume.mutate()} loading={resume.isPending}>
          Resume
        </Button>
      )}
      <ErrorAlert error={resume.error} />
    </span>
  );
}
