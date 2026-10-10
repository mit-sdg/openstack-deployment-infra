import type { AppRecord, Deployment } from '../api';

export function time(value: string | null | undefined) {
  return value
    ? new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
    : '—';
}

export function short(value: string | null | undefined) {
  return value ? value.slice(0, 9) : 'No deployment';
}

/** "Node.js 22.11.0 · from engines.node >=22 <23", or "Bun (platform default)". */
export function deploymentRuntime(deployment: Pick<Deployment, 'configuration' | 'runtime'>) {
  const runtime = deployment.runtime;
  const name =
    (runtime?.runtime ?? deployment.configuration.build.runtime) === 'bun' ? 'Bun' : 'Node.js';
  if (!runtime) return name;
  if (runtime.version === null) return `${name} (platform default)`;
  // "packageManager bun@1.3.4" would repeat the version.
  const source = runtime.source.startsWith('packageManager ') ? 'packageManager' : runtime.source;
  return `${name} ${runtime.version} · from ${source}`;
}

/**
 * The one user-facing state of an app, for every page and role. Lifecycle
 * comes first (creating / not created), then whether it ever went live, then
 * runtime: stopped, healthy, unhealthy or unknown (stale or unobserved).
 * "Ready" is a lifecycle detail and is never shown for an app.
 */
export type AppState =
  'creating' | 'rejected' | 'not_deployed' | 'stopped' | 'healthy' | 'unhealthy' | 'unknown';
export function appState(app: {
  lifecycle: string;
  deployed: boolean;
  running: boolean;
  stale: boolean;
  health: 'healthy' | 'unhealthy' | 'unknown';
}): AppState {
  if (app.lifecycle === 'creating') return 'creating';
  if (app.lifecycle === 'rejected') return 'rejected';
  if (!app.deployed) return 'not_deployed';
  if (!app.running) return 'stopped';
  if (app.stale) return 'unknown';
  return app.health;
}
/** appState for an owner or admin app record. */
export function ownerAppState(app: AppRecord): AppState {
  const { routeHealthy, allocationHealthy } = app.health ?? {};
  return appState({
    lifecycle: app.lifecycleState,
    deployed: !!app.acceptedDeployment,
    running: app.desiredRunning,
    stale: app.stale,
    health:
      routeHealthy && allocationHealthy
        ? 'healthy'
        : routeHealthy === false || allocationHealthy === false
          ? 'unhealthy'
          : 'unknown',
  });
}

/** @deprecated Use ownerAppState, which also covers lifecycle and first deploy. */
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
// Failed or Needs attention badge. Keys are exactly the broker's INTENT_KINDS
// (openstack_platform/management/broker/class_reads.py); a test keeps them in sync.
export const activityTitles: Record<string, [done: string, running: string, noun: string]> = {
  create_app: ['App created', 'Creating app', 'App creation'],
  save_configuration: ['Settings saved', 'Saving settings', 'Settings change'],
  builder_size: ['Build machine saved', 'Saving build machine', 'Build machine change'],
  default_builder_size: [
    'Default build machine saved',
    'Saving default build machine',
    'Default build machine change',
  ],
  deploy: ['Deployed', 'Deploying', 'Deployment'],
  env_set: ['Variable set', 'Setting variable', 'Variable change'],
  env_delete: ['Variable deleted', 'Deleting variable', 'Variable deletion'],
  storage_create: ['Storage added', 'Adding storage', 'Storage creation'],
  storage_verify: ['Storage checked', 'Checking storage', 'Storage check'],
  storage_rotate: ['Credentials rotated', 'Rotating credentials', 'Credential rotation'],
  storage_limits: ['Storage limits saved', 'Saving storage limits', 'Storage limits change'],
  storage_delete: ['Storage deleted', 'Deleting storage', 'Storage deletion'],
  adopt_app: ['App adopted', 'Adopting app', 'App adoption'],
  app_enable: ['App started', 'Starting app', 'App start'],
  app_disable: ['App stopped', 'Stopping app', 'App stop'],
  app_restart: ['App restarted', 'Restarting app', 'App restart'],
};
const otherChange: [string, string, string] = ['Change made', 'Making change', 'Change'];
export function activityTitle(kind: string, state: string) {
  const [done, running, noun] = activityTitles[kind] ?? otherChange;
  if (state === 'succeeded') return done;
  if (['failed', 'blocked', 'unknown', 'rejected'].includes(state)) return noun;
  return running;
}
