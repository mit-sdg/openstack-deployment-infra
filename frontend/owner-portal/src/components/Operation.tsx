import { Button, List, ListItem } from '@openstack-platform/ui';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { Link } from 'wouter';
import { api, type Intent } from '../api';
import { humanPhase, relativeTime, short } from '../utils/presentation';
import { ErrorNotice } from './Feedback';
import { Status } from './Status';

const titles: Record<string, string> = {
  deploy: 'Deploy',
  create_app: 'Create app',
  storage_create: 'Add storage',
  storage_verify: 'Check storage',
  storage_rotate: 'Rotate storage credentials',
  env_set: 'Set environment variable',
  env_delete: 'Delete environment variable',
};
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
  const title = titles[intent.kind] ?? 'Save settings';
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
          <time dateTime={intent.createdAt} title={new Date(intent.createdAt).toLocaleString()}>
            {relativeTime(intent.createdAt)}
          </time>
        </>
      }
      trailing={
        <>
          {intent.state === 'blocked' && !intent.requiresResubmit && (
            <Button size="sm" onClick={() => resume.mutate()} loading={resume.isPending}>
              Resume
            </Button>
          )}
          <Status state={intent.state} />
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
        </p>
      )}
      <ErrorNotice error={resume.error} />
    </ListItem>
  );
}

export function OperationList({ label, children }: { label: string; children: ReactNode }) {
  return <List label={label}>{children}</List>;
}
