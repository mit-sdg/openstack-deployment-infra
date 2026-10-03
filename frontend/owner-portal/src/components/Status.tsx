import { Badge, StatusText, type Tone } from '@openstack-platform/ui';

// One vocabulary for app, deployment, account and activity states.
const labels: Record<string, string> = {
  // Expected states (no badge; see `quiet` below).
  succeeded: 'Succeeded',
  healthy: 'Healthy',
  ready: 'Ready',
  active: 'Active',
  live: 'Live',
  // In progress.
  accepted: 'In progress',
  prepared: 'Queued',
  queued: 'Queued',
  creating: 'Creating',
  building: 'Building',
  deploying: 'Deploying',
  running: 'Running',
  // Needs attention.
  failed: 'Failed',
  blocked: 'Needs attention',
  recovery_required: 'Needs attention',
  unhealthy: 'Unhealthy',
  rejected: 'Not created',
  // Neutral.
  unknown: 'Unknown',
  stopped: 'Stopped',
  disabled: 'Disabled',
  rolled_back: 'Rolled back',
  not_deployed: 'Not deployed',
};
const tones: Record<string, Tone> = {
  succeeded: 'success',
  healthy: 'success',
  ready: 'success',
  active: 'success',
  live: 'success',
  accepted: 'info',
  prepared: 'info',
  queued: 'info',
  creating: 'info',
  building: 'info',
  deploying: 'info',
  running: 'info',
  failed: 'danger',
  blocked: 'danger',
  recovery_required: 'danger',
  unhealthy: 'danger',
  rejected: 'danger',
};
/** The normal, expected states. They never get a badge. */
export const defaultStates = new Set(['succeeded', 'healthy', 'ready', 'active', 'live']);

/**
 * A state as a badge when it needs attention, otherwise quietly:
 * - `quiet="text"` (default): a dot and muted text, for tables and details.
 * - `quiet="hidden"`: nothing visible, still read by screen readers, for feeds.
 */
export function Status({
  state,
  label,
  quiet = 'text',
}: {
  state: string;
  label?: string;
  quiet?: 'text' | 'hidden';
}) {
  const text = label ?? labels[state] ?? state;
  if (defaultStates.has(state))
    return quiet === 'hidden' ? (
      <span className="ui-sr-only">{text}</span>
    ) : (
      <StatusText tone={tones[state]}>{text}</StatusText>
    );
  return <Badge tone={tones[state] ?? 'neutral'}>{text}</Badge>;
}
