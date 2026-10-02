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

export function relativeTime(value: string, now = Date.now()) {
  const seconds = Math.round((new Date(value).getTime() - now) / 1000);
  if (Math.abs(seconds) < 60) return 'just now';
  const formatter = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });
  if (Math.abs(seconds) < 3600) return formatter.format(Math.trunc(seconds / 60), 'minute');
  if (Math.abs(seconds) < 86400) return formatter.format(Math.trunc(seconds / 3600), 'hour');
  return formatter.format(Math.trunc(seconds / 86400), 'day');
}
export function humanPhase(phase: string) {
  const labels: Record<string, string> = {
    finished: 'Completed',
    accepted: 'Health checks passed',
    building: 'Building exact snapshot',
    build_rejected: 'Build rejected; cleanup confirmed',
    queued: 'Waiting for a build slot',
    executing: 'Starting deployment',
    startup_interrupted: 'Recovery required',
  };
  return (
    labels[phase] ?? phase.replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase())
  );
}
