import {
  Button,
  CodeBlock,
  EmptyState,
  Hint,
  LoadError,
  LoadingRows,
  RelativeTime,
  Section,
  SegmentedControl,
} from '@openstack-platform/ui';
import { useQuery } from '@tanstack/react-query';
import { useId, useState } from 'react';
import { ApiError, type LogStream, type RuntimeLog } from '../api';
import { loadErrorMessage } from './Feedback';

const streams: { value: LogStream; label: string }[] = [
  { value: 'stdout', label: 'Output' },
  { value: 'stderr', label: 'Errors' },
];

// Shown as the server says them: what happened and what to do.
const explained = new Set(['LOG_STREAM_UNAVAILABLE', 'LOGS_BUSY']);

/**
 * Recent output of a running app. Each read asks the platform for fresh
 * output, so it refreshes on request rather than on a timer.
 */
export function LogViewer({
  queryKey,
  read,
  idle,
}: {
  queryKey: readonly unknown[];
  read: (stream: LogStream) => Promise<RuntimeLog>;
  /** Text under "No logs right now", for the person viewing. */
  idle: string;
}) {
  const [stream, setStream] = useState<LogStream>('stdout');
  const name = useId();
  const log = useQuery({ queryKey: [...queryKey, stream], queryFn: () => read(stream) });
  const label = stream === 'stdout' ? 'App output' : 'App errors';
  return (
    <Section
      title="Logs"
      actions={
        <Button
          size="sm"
          variant="ghost"
          loading={log.isFetching && !log.isPending}
          disabled={log.isPending}
          onClick={() => void log.refetch()}
        >
          Refresh
        </Button>
      }
    >
      <SegmentedControl
        label="Stream"
        hideLabel
        name={name}
        options={streams}
        value={stream}
        onChange={setStream}
      />
      {log.isPending ? (
        <LoadingRows />
      ) : log.error ? (
        <LoadError onRetry={() => void log.refetch()} retrying={log.isFetching}>
          {log.error instanceof ApiError && explained.has(log.error.code)
            ? log.error.message
            : loadErrorMessage(log.error, 'the logs')}
        </LoadError>
      ) : !log.data.running ? (
        <EmptyState title="No logs right now">{idle}</EmptyState>
      ) : (
        <>
          <CodeBlock label={label} variant="log" end>
            {log.data.text.replace(/\x1b\[[0-9;]*[A-Za-z]/g, '') || 'Nothing printed yet.'}
          </CodeBlock>
          <Hint>
            {log.data.truncated || log.data.text.split('\n').length > log.data.lines
              ? `The last ${log.data.lines} lines, as of `
              : 'As of '}
            <RelativeTime value={log.data.observedAt} />. Logs can include anything the app prints,
            so share them with care.
          </Hint>
        </>
      )}
    </Section>
  );
}
