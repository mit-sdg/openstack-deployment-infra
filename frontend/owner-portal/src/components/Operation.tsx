import { ActivityRow } from '@openstack-platform/ui';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from 'wouter';
import { api, type Intent } from '../api';
import { humanPhase, relativeTime, short } from '../utils/presentation';
import { ErrorNotice } from './Feedback';
import { Status } from './Status';

export function Operation({ intent }: { intent: Intent }) {
  const client = useQueryClient();
  const resume = useMutation({
    mutationFn: () => api.resume(intent.intentId),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ['intents'] });
      client.invalidateQueries({ queryKey: ['intent', intent.intentId] });
    },
  });
  const title =
    intent.kind === 'deploy'
      ? 'Deploy application'
      : intent.kind === 'create_app'
        ? 'Create application'
        : ((
            {
              storage_create: 'Create storage',
              storage_verify: 'Verify storage',
              storage_rotate: 'Rotate storage credentials',
              env_set: 'Set environment variable',
              env_delete: 'Delete environment variable',
            } as Record<string, string>
          )[intent.kind] ?? 'Save configuration');
  return (
    <ActivityRow>
      <div>
        <strong>{title}</strong>
        <div className="operation-subject">
          <Link href={`/apps/${intent.appId}`} className="app-link">
            {intent.appSlug ?? 'Application'}
          </Link>
          {intent.commit && <code>{short(intent.commit)}</code>}
          <time dateTime={intent.createdAt} title={new Date(intent.createdAt).toLocaleString()}>
            {relativeTime(intent.createdAt)}
          </time>
        </div>
        <p className="muted">
          {intent.operation?.phase
            ? humanPhase(intent.operation.phase)
            : intent.state === 'unknown'
              ? intent.requiresResubmit
                ? 'Resubmit the original environment edit to recover'
                : 'Recovering the original request'
              : 'Recorded in your workspace'}
        </p>
        {intent.safeError && <p className="operation-error">{intent.safeError}</p>}
        {intent.controllerErrorCode && (
          <p className="operation-error">
            Controller code: <code>{intent.controllerErrorCode}</code>
          </p>
        )}
        {intent.operationId && intent.kind === 'deploy' && (
          <Link
            href={`/apps/${intent.appId}/deployments/${intent.operationId}`}
            className="text-link"
          >
            View deployment →
          </Link>
        )}
        <ErrorNotice error={resume.error} />
      </div>
      <div className="operation-actions">
        <Status state={intent.state} />
        {intent.state === 'blocked' && !intent.requiresResubmit && (
          <button
            className="button button-small"
            onClick={() => resume.mutate()}
            disabled={resume.isPending}
          >
            Resume
          </button>
        )}
      </div>
    </ActivityRow>
  );
}
