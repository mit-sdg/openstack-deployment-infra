export function Status({ state }: { state: string }) {
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
    stopped: 'Not running',
  };
  const tone = ['succeeded', 'healthy'].includes(state)
    ? 'good'
    : ['failed', 'blocked'].includes(state)
      ? 'critical'
      : ['accepted', 'running', 'prepared', 'creating'].includes(state)
        ? 'info'
        : 'neutral';
  return (
    <span className={`status tone-${tone}`}>
      <span className="status-dot" aria-hidden="true" />
      {label[state] ?? state}
    </span>
  );
}
