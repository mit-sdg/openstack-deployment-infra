import { Badge, type Tone } from '@openstack-platform/ui';

// One vocabulary for app, deployment and activity states.
const labels: Record<string, string> = {
  succeeded: 'Succeeded',
  failed: 'Failed',
  blocked: 'Needs attention',
  accepted: 'In progress',
  prepared: 'Queued',
  unknown: 'Unknown',
  ready: 'Ready',
  creating: 'Creating',
  running: 'Running',
  healthy: 'Healthy',
  unhealthy: 'Unhealthy',
  stopped: 'Stopped',
};
const tones: Record<string, Tone> = {
  succeeded: 'success',
  healthy: 'success',
  ready: 'success',
  failed: 'danger',
  blocked: 'danger',
  unhealthy: 'danger',
  accepted: 'info',
  running: 'info',
  prepared: 'info',
  creating: 'info',
};

export function Status({ state, label }: { state: string; label?: string }) {
  return <Badge tone={tones[state] ?? 'neutral'}>{label ?? labels[state] ?? state}</Badge>;
}
