import { StatusBadge } from '@openstack-platform/ui';
export function Status({ state, label: customLabel }: { state: string; label?: string }) {
  const label: Record<string, string> = {
    succeeded: 'Succeeded',
    failed: 'Failed',
    blocked: 'Recovery required',
    accepted: 'In progress',
    prepared: 'Queued',
    unknown: 'Reconnecting',
    ready: 'Ready',
    creating: 'Creating',
    running: 'Running',
    healthy: 'Healthy',
    unhealthy: 'Unhealthy',
    stopped: 'Not running',
  };
  const tone = ['succeeded', 'healthy'].includes(state)
    ? 'good'
    : ['failed', 'blocked', 'unhealthy'].includes(state)
      ? 'critical'
      : ['accepted', 'running', 'prepared', 'creating'].includes(state)
        ? 'info'
        : 'neutral';
  return <StatusBadge label={customLabel ?? label[state] ?? state} tone={tone} />;
}
