import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { Configuration, StartupRecord } from '../api';
import { StartupRecordSection, startupSummary } from './StartupRecord';

const configuration: Configuration = {
  schemaVersion: 1,
  build: { runtime: 'node', packages: ['.'], buildScript: null, startScript: 'start' },
  runtime: { port: 3000, healthPath: '/health' },
  storageBindings: [],
};
const event = (type: string, message = '', exitCode: number | null = null, oomKilled = false) => ({
  type,
  message,
  exitCode,
  oomKilled,
});
const record = (extra: Partial<StartupRecord>): StartupRecord => ({
  captured: true,
  found: true,
  clientStatus: 'failed',
  restarts: 0,
  events: [],
  stdout: '',
  stderr: '',
  capturedAt: new Date().toISOString(),
  ...extra,
});

describe('why a deployment stopped', () => {
  it('names the cause from the task events', () => {
    expect(startupSummary(record({ found: false }), configuration)).toBe(
      'Your app never got a place to run.',
    );
    expect(
      startupSummary(
        record({ restarts: 3, events: [event('Terminated', 'Exit Code: 137', 137, true)] }),
        configuration,
      ),
    ).toBe('Your app ran out of memory and was stopped. It was restarted 3 times first.');
    expect(
      startupSummary(
        record({
          restarts: 1,
          events: [event('Terminated', 'Exit Code: 0', 0), event('Terminated', 'Exit Code: 1', 1)],
        }),
        configuration,
      ),
    ).toBe('Your app exited with code 1. It was restarted 1 time first.');
    expect(startupSummary(record({ events: [event('Terminated', '', 0)] }), configuration)).toBe(
      'Your app exited on its own. It must keep running and serve requests.',
    );
    expect(startupSummary(record({ clientStatus: 'running' }), configuration)).toBe(
      'Your app started but didn’t pass the health check: /health on port 3000 has to return HTTP 2xx.',
    );
  });

  it('opens on the errors when there are any, and shows the events', () => {
    render(
      <StartupRecordSection
        record={record({
          stdout: '> start\n',
          stderr: "\x1b[31mError:\x1b[0m Cannot find module 'express'\n",
          events: [event('Terminated', 'Exit Code: 1', 1), event('Restarting', '')],
        })}
        configuration={configuration}
      />,
    );
    expect(screen.getByText('Your app exited with code 1.')).toBeVisible();
    expect(screen.getByLabelText('Startup errors')).toHaveTextContent(
      "Error: Cannot find module 'express'",
    );
    expect(screen.getByRole('list', { name: 'Events' })).toHaveTextContent(
      'Terminated Exit Code: 1',
    );
    expect(screen.getByRole('list', { name: 'Events' })).not.toHaveTextContent('Restarting');
    fireEvent.click(screen.getByRole('radio', { name: 'Output' }));
    expect(screen.getByLabelText('Startup output')).toHaveTextContent('> start');
  });
});
