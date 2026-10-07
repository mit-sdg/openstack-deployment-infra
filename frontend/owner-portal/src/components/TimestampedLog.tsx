import { Button, Cluster, CodeBlock, Hint, Switch } from '@openstack-platform/ui';
import { useEffect, useState } from 'react';

const prefix = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z) ([\s\S]*)$/;

/** Accept only the platform's UTC millisecond prefix, leaving legacy lines intact. */
export function parseLogLine(line: string) {
  const match = prefix.exec(line);
  if (match) {
    const date = new Date(match[1]);
    if (Number.isFinite(date.getTime()) && date.toISOString() === match[1])
      return { timestamp: match[1], message: match[2] };
  }
  return { timestamp: null, message: line };
}

const timeFormat = new Intl.DateTimeFormat(undefined, {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  fractionalSecondDigits: 3,
  hourCycle: 'h23',
});
const localTime = (timestamp: string) => timeFormat.format(new Date(timestamp));

/** Runtime, startup and build output share timestamp display and raw exports. */
export function TimestampedLog({
  label,
  text,
  empty,
  end = false,
}: {
  label: string;
  text: string;
  empty: string;
  end?: boolean;
}) {
  const [showTimes, setShowTimes] = useState(true);
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  useEffect(() => setCopyState('idle'), [text, label]);
  const clean = text.replace(/\x1b\[[0-9;]*[A-Za-z]/g, '');
  const lines = clean.replace(/\n$/, '').split('\n').map(parseLogLine);
  const hasTimes = lines.some((line) => line.timestamp !== null);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(clean);
      setCopyState('copied');
    } catch {
      setCopyState('failed');
    }
  };
  const download = () => {
    const url = URL.createObjectURL(new Blob([clean], { type: 'text/plain;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `${label.toLowerCase().replace(/\s+/g, '-')}.log`;
    link.click();
    URL.revokeObjectURL(url);
  };
  return (
    <>
      <Cluster>
        {hasTimes && (
          <Switch
            label="Show timestamps"
            checked={showTimes}
            onChange={(event) => setShowTimes(event.target.checked)}
          />
        )}
        <Button size="sm" variant="ghost" disabled={!clean} onClick={() => void copy()}>
          {copyState === 'copied' ? 'Copied' : 'Copy logs'}
        </Button>
        <Button size="sm" variant="ghost" disabled={!clean} onClick={download}>
          Download logs
        </Button>
      </Cluster>
      {copyState === 'failed' && <Hint>Couldn’t copy the logs. Download them instead.</Hint>}
      <CodeBlock label={label} variant="log" end={end}>
        {clean
          ? lines.map((line, index) => (
              <span className="app-log-line" key={index}>
                {showTimes && hasTimes && (
                  <span className="app-log-time ui-text-muted">
                    {line.timestamp && (
                      <time dateTime={line.timestamp} title={line.timestamp}>
                        {localTime(line.timestamp)}
                      </time>
                    )}
                  </span>
                )}
                <span className="app-log-message">{line.message || '\u200b'}</span>
              </span>
            ))
          : empty}
      </CodeBlock>
    </>
  );
}
