import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, expect, it } from 'vitest';
import { BoundaryText } from './BoundaryText';
import { Operation } from './Operation';
import { ThemeButton } from './ThemeButton';
import { Status } from './Status';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import {
  activityTitle,
  activityTitles,
  appState,
  humanPhase,
  relativeTime,
} from '../utils/presentation';

describe('reviewed owner presentation', () => {
  it('renders activity with app, commit, relative time and humanized progress', () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ul>
          <Operation
            intent={{
              intentId: 'intent',
              appId: 'app',
              appSlug: 'student-project',
              commit: 'a'.repeat(40),
              createdAt: new Date().toISOString(),
              kind: 'deploy',
              state: 'accepted',
              operationId: null,
              operation: { status: 'running', phase: 'build_rejected', cleanupState: 'pending' },
              safeError: null,
              controllerErrorCode: 'INVALID_REQUEST',
            }}
          />
        </ul>
      </QueryClientProvider>,
    );
    expect(screen.getByText('student-project')).toBeVisible();
    expect(screen.getByText('aaaaaaaaa')).toBeVisible();
    expect(screen.getByText('just now')).toBeVisible();
    expect(screen.getByText('Build failed')).toBeVisible();
    expect(screen.getByText('In progress')).toBeVisible();
    expect(screen.getByText('INVALID_REQUEST')).toBeVisible();
    expect(screen.queryByText(/build_rejected/)).toBeNull();
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
  it('badges only states that need attention', () => {
    const { container } = render(
      <>
        <Status state="healthy" />
        <Status state="live" />
        <Status state="succeeded" quiet="hidden" />
        <Status state="building" />
        <Status state="recovery_required" />
        <Status state="rolled_back" />
      </>,
    );
    const badges = [...container.querySelectorAll('.ui-badge')].map((badge) => badge.textContent);
    expect(badges).toEqual(['Building', 'Needs attention', 'Rolled back']);
    expect(screen.getByText('Healthy')).toHaveClass('ui-status-text');
    expect(screen.getByText('Live')).toHaveClass('ui-status-text');
    expect(screen.getByText('Succeeded')).toHaveClass('ui-sr-only');
  });
  it('titles activity as events phrased by outcome', () => {
    expect(activityTitle('deploy', 'succeeded')).toBe('Deployed');
    expect(activityTitle('deploy', 'accepted')).toBe('Deploying');
    expect(activityTitle('deploy', 'failed')).toBe('Deployment');
    expect(activityTitle('create_app', 'succeeded')).toBe('App created');
    expect(activityTitle('env_set', 'succeeded')).toBe('Variable set');
    expect(activityTitle('something_new', 'succeeded')).toBe('Change made');
  });
  it('titles every intent kind the broker records', () => {
    // INTENT_KINDS in the broker is the source of truth for activity kinds.
    // Tests run from the owner-portal workspace directory.
    const source = readFileSync(
      resolve(process.cwd(), '../../openstack_platform/management/broker/class_reads.py'),
      'utf8',
    );
    const block = source.slice(source.indexOf('INTENT_KINDS = frozenset('));
    const kinds = [...block.slice(0, block.indexOf(')')).matchAll(/"([a-z_]+)"/g)].map(
      (match) => match[1],
    );
    expect(kinds.length).toBeGreaterThanOrEqual(12);
    expect(Object.keys(activityTitles).sort()).toEqual([...kinds].sort());
  });
  it('gives every app one state word, in lifecycle, deploy, runtime order', () => {
    const app = {
      lifecycle: 'ready',
      deployed: true,
      running: true,
      stale: false,
      health: 'healthy' as const,
    };
    expect(appState({ ...app, lifecycle: 'creating' })).toBe('creating');
    expect(appState({ ...app, lifecycle: 'rejected' })).toBe('rejected');
    expect(appState({ ...app, deployed: false })).toBe('not_deployed');
    expect(appState({ ...app, running: false })).toBe('stopped');
    expect(appState({ ...app, stale: true })).toBe('unknown');
    expect(appState({ ...app, health: 'unhealthy' })).toBe('unhealthy');
    expect(appState(app)).toBe('healthy');
  });
});
