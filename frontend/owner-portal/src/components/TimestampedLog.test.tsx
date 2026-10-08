import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { parseLogLine, TimestampedLog } from './TimestampedLog';

const timestamp = '2026-10-07T18:36:05.123Z';
const text = `${timestamp} <b>app output</b>\nold output\n`;

describe('timestamped output', () => {
  it('parses only a valid leading UTC millisecond timestamp', () => {
    expect(parseLogLine(`${timestamp} message`)).toEqual({ timestamp, message: 'message' });
    expect(parseLogLine(`${timestamp} CRLF\r`)).toEqual({ timestamp, message: 'CRLF\r' });
    expect(parseLogLine(`${timestamp} `)).toEqual({ timestamp, message: '' });
    for (const line of [
      'old output',
      `${timestamp}message`,
      `message ${timestamp}`,
      '2026-02-30T18:36:05.123Z invalid',
      '2026-10-07T99:36:05.123Z invalid',
    ])
      expect(parseLogLine(line)).toEqual({ timestamp: null, message: line });
  });

  it('renders local time in a separate column, preserves legacy text and toggles times', () => {
    render(<TimestampedLog label="App output" text={text} empty="Empty" />);
    const log = screen.getByLabelText('App output');
    const time = log.querySelector('time');
    expect(time).toHaveAttribute('datetime', timestamp);
    expect(time).toHaveAttribute('title', timestamp);
    expect(time).toHaveTextContent(
      new Intl.DateTimeFormat(undefined, {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        fractionalSecondDigits: 3,
        hourCycle: 'h23',
      }).format(new Date(timestamp)),
    );
    expect(log).toHaveTextContent('old output');
    expect(log.querySelector('b')).toBeNull();
    fireEvent.click(screen.getByRole('switch', { name: 'Show timestamps' }));
    expect(log.querySelector('time')).toBeNull();
    expect(log).toHaveTextContent('<b>app output</b>');
  });

  it('copies and downloads original UTC prefixes even when hidden', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const create = vi.fn().mockReturnValue('blob:logs');
    const revoke = vi.fn();
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: create });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revoke });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    render(<TimestampedLog label="App output" text={text} empty="Empty" />);
    fireEvent.click(screen.getByRole('switch', { name: 'Show timestamps' }));
    fireEvent.click(screen.getByRole('button', { name: 'Copy logs' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(text));
    fireEvent.click(screen.getByRole('button', { name: 'Download logs' }));
    expect(create).toHaveBeenCalledWith(expect.any(Blob));
    const reader = new FileReader();
    const downloaded = new Promise((resolve) => {
      reader.onload = () => resolve(reader.result);
    });
    reader.readAsText(create.mock.calls[0][0]);
    expect(await downloaded).toBe(text);
    expect(click).toHaveBeenCalled();
    expect(revoke).toHaveBeenCalledWith('blob:logs');
    click.mockRestore();
  });

  it('leaves legacy lines readable without a timestamp toggle', () => {
    render(<TimestampedLog label="App errors" text={'legacy\n\nlast'} empty="Empty" />);
    expect(screen.queryByRole('switch')).toBeNull();
    expect(screen.getByLabelText('App errors').querySelectorAll('.app-log-line')).toHaveLength(3);
  });
});
