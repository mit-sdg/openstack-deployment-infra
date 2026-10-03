import type { AppRecord } from '../api';

export function time(value: string | null | undefined) {
  return value
    ? new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
    : '—';
}

export function short(value: string | null | undefined) {
  return value ? value.slice(0, 9) : 'No deployment';
}

export function healthy(app: AppRecord) {
  return app.stale
    ? 'unknown'
    : !app.desiredRunning
      ? 'stopped'
      : app.health?.routeHealthy && app.health?.allocationHealthy
        ? 'healthy'
        : 'unknown';
}

// Shared with the design system so every page formats times the same way.
export { relativeTime } from '@openstack-platform/ui';
export function humanPhase(phase: string) {
  const labels: Record<string, string> = {
    finished: 'Completed',
    accepted: 'Health checks passed',
    building: 'Building',
    build_rejected: 'Build failed',
    queued: 'Waiting to build',
    executing: 'Starting',
    startup_interrupted: 'Interrupted',
  };
  return (
    labels[phase] ?? phase.replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase())
  );
}

// Activity titles are events, phrased by outcome: "Deployed" when it
// succeeded, "Deploying" while it runs, and the noun ("Deployment") next to a
// Failed or Needs attention badge. Unknown kinds read as a settings change.
const activityTitles: Record<string, [done: string, running: string, noun: string]> = {
  deploy: ['Deployed', 'Deploying', 'Deployment'],
  create_app: ['App created', 'Creating app', 'App creation'],
  save_configuration: ['Settings saved', 'Saving settings', 'Settings change'],
  configure: ['Settings saved', 'Saving settings', 'Settings change'],
  env_set: ['Variable set', 'Setting variable', 'Variable change'],
  env_delete: ['Variable deleted', 'Deleting variable', 'Variable deletion'],
  storage_create: ['Storage added', 'Adding storage', 'Storage creation'],
  storage_verify: ['Storage checked', 'Checking storage', 'Storage check'],
  storage_rotate: ['Credentials rotated', 'Rotating credentials', 'Credential rotation'],
  storage_delete: ['Storage deleted', 'Deleting storage', 'Storage deletion'],
  lifecycle: ['App state changed', 'Changing app state', 'App state change'],
  adopt_app: ['App adopted', 'Adopting app', 'App adoption'],
};
export function activityTitle(kind: string, state: string) {
  const [done, running, noun] = activityTitles[kind] ?? activityTitles.save_configuration;
  if (state === 'succeeded') return done;
  if (['failed', 'blocked', 'unknown', 'rejected'].includes(state)) return noun;
  return running;
}
