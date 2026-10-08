import { Hint, RelativeTime, Section, SegmentedControl } from '@openstack-platform/ui';
import { useId, useState } from 'react';
import type { Configuration, StartupRecord } from '../api';
import { TimestampedLog } from './TimestampedLog';

/** One plain sentence on why the new version stopped, from its task events. */
export function startupSummary(record: StartupRecord, configuration: Configuration) {
  const events = record.events ?? [];
  const restarts = record.restarts
    ? ` It was restarted ${record.restarts} time${record.restarts === 1 ? '' : 's'} first.`
    : '';
  if (!record.found) return 'Your app never got a place to run.';
  if (events.some((event) => event.oomKilled || /\bOOM\b/i.test(event.message)))
    return `Your app ran out of memory and was stopped.${restarts}`;
  const exit = [...events]
    .reverse()
    .find((event) => event.type === 'Terminated' && event.exitCode !== null);
  if (exit && exit.exitCode !== 0) return `Your app exited with code ${exit.exitCode}.${restarts}`;
  if (exit)
    return `Your app exited on its own. It must keep running and serve requests.${restarts}`;
  if (events.some((event) => event.type === 'Driver Failure'))
    return 'Your app couldn’t be started on its server.';
  const { port, healthPath } = configuration.runtime;
  if (record.clientStatus === 'running')
    return `Your app started but didn’t pass the health check: ${healthPath} on port ${port} has to return HTTP 2xx.`;
  return 'Your app didn’t become healthy in time.';
}

/** Why a failed deployment's new version stopped, with what it printed. */
export function StartupRecordSection({
  record,
  configuration,
}: {
  record: StartupRecord;
  configuration: Configuration;
}) {
  const name = useId();
  const [stream, setStream] = useState<'stdout' | 'stderr'>(
    record.stderr?.trim() ? 'stderr' : 'stdout',
  );
  const text = (stream === 'stdout' ? record.stdout : record.stderr) ?? '';
  const events = (record.events ?? []).filter((event) => event.message);
  return (
    <Section title="Why it stopped">
      <p className="app-startup-summary">{startupSummary(record, configuration)}</p>
      {record.found && (
        <>
          <SegmentedControl
            label="Stream"
            hideLabel
            name={name}
            options={[
              { value: 'stdout', label: 'Output' },
              { value: 'stderr', label: 'Errors' },
            ]}
            value={stream}
            onChange={setStream}
          />
          <TimestampedLog
            label={stream === 'stdout' ? 'Startup output' : 'Startup errors'}
            text={text}
            empty="Nothing printed."
            end
          />
        </>
      )}
      {events.length > 0 && (
        <ul className="app-startup-events ui-text-sm ui-text-muted" aria-label="Events">
          {events.map((event, index) => (
            <li key={index}>
              <span className="app-startup-event">{event.type}</span> {event.message}
            </li>
          ))}
        </ul>
      )}
      <Hint>
        Saved when this version was removed
        {record.capturedAt ? (
          <>
            {', '}
            <RelativeTime value={record.capturedAt} />
          </>
        ) : null}
        . It can include anything your app printed, so share it with care.
      </Hint>
    </Section>
  );
}
