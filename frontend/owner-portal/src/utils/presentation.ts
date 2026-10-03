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
