import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, expect, it } from 'vitest';
import { BoundaryText } from './BoundaryText';
import { Operation } from './Operation';
import { ThemeButton } from './ThemeButton';
import { humanPhase, relativeTime } from '../utils/presentation';

describe('reviewed owner presentation', () => {
  it('renders activity with app, commit, relative time and humanized progress', () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Operation
          intent={{
            intentId: 'intent',
            appId: 'app',
            appSlug: 'student-project',
            commit: 'a'.repeat(40),
            createdAt: new Date().toISOString(),
            kind: 'deploy',
            state: 'succeeded',
            operationId: null,
            operation: { status: 'succeeded', phase: 'finished', cleanupState: 'confirmed' },
            safeError: null,
          }}
        />
      </QueryClientProvider>,
    );
    expect(screen.getByText('student-project')).toBeVisible();
    expect(screen.getByText('aaaaaaaaa')).toBeVisible();
    expect(screen.getByText('just now')).toBeVisible();
    expect(screen.getByText('Completed')).toBeVisible();
    expect(screen.queryByText('finished')).toBeNull();
  });
  it('places explicit wrap opportunities at URL separators', () => {
    const { container } = render(
      <BoundaryText text="https://github.com/example/student-project" />,
    );
    expect(container.textContent).toBe('https://github.com/example/student-project');
    expect(container.querySelectorAll('wbr')).toHaveLength(5);
  });
  it('uses a dashboard-style SVG theme icon', () => {
    const { container } = render(<ThemeButton />);
    expect(screen.getByRole('button', { name: /Theme:/ })).toBeVisible();
    expect(container.querySelector('svg')).not.toBeNull();
  });
  it('formats relative times and unknown phases without raw underscores', () => {
    expect(relativeTime('2026-10-02T10:00:00Z', Date.parse('2026-10-02T10:05:00Z'))).toBe(
      '5 minutes ago',
    );
    expect(humanPhase('image_pushed')).toBe('Image pushed');
  });
});
