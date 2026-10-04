import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ApiError, type LogStream, type RuntimeLog } from '../api';
import { LogViewer } from './LogViewer';

function log(stream: LogStream, text: string, extra: Partial<RuntimeLog> = {}): RuntimeLog {
  return {
    stream,
    running: true,
    text,
    truncated: false,
    lines: 500,
    observedAt: new Date().toISOString(),
    ...extra,
  };
}
function show(read: (stream: LogStream) => Promise<RuntimeLog>) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LogViewer queryKey={['logs', 'app']} read={read} idle="Deploy it to see logs." />
    </QueryClientProvider>,
  );
}

describe('app logs', () => {
  it('shows output, switches to errors and refreshes on request', async () => {
    const read = vi.fn((stream: LogStream) =>
      Promise.resolve(
        stream === 'stdout'
          ? log('stdout', '\x1b[32mListening\x1b[0m on 3000\n')
          : log('stderr', 'Warning: slow query\n'),
      ),
    );
    show(read);
    expect(await screen.findByLabelText('App output')).toHaveTextContent('Listening on 3000');
    expect(screen.getByText(/share them with care/)).toHaveTextContent(/^As of/);
    fireEvent.click(screen.getByRole('radio', { name: 'Errors' }));
    expect(await screen.findByLabelText('App errors')).toHaveTextContent('Warning: slow query');
    expect(read).toHaveBeenLastCalledWith('stderr');
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(read).toHaveBeenCalledTimes(3));
  });

  it('says when nothing runs, when output was cut and when a stream is unavailable', async () => {
    const read = vi.fn((stream: LogStream) =>
      stream === 'stdout'
        ? Promise.resolve(log('stdout', '', { running: false }))
        : Promise.reject(
            new ApiError(409, 'LOG_STREAM_UNAVAILABLE', "Error output isn't available yet."),
          ),
    );
    show(read);
    expect(await screen.findByText('No logs right now')).toBeVisible();
    expect(screen.getByText('Deploy it to see logs.')).toBeVisible();
    fireEvent.click(screen.getByRole('radio', { name: 'Errors' }));
    expect(await screen.findByText("Error output isn't available yet.")).toBeVisible();

    const cut = vi.fn(() => Promise.resolve(log('stdout', 'tail\n', { truncated: true })));
    show(cut);
    expect(await screen.findByText(/The last 500 lines, as of/)).toBeVisible();
  });
});
