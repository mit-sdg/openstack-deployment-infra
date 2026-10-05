import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, type Deployment } from '../api';
import { deploymentRuntime } from '../utils/presentation';
import { DeploymentPage } from './Deployment';

const deployment: Deployment = {
  deploymentId: '44444444-4444-4444-8444-444444444444',
  applicationId: '33333333-3333-4333-8333-333333333333',
  status: 'succeeded',
  repositoryCommit: 'a'.repeat(40),
  sourceRepository: 'https://github.com/ada/app',
  configurationRevision: 1,
  configuration: {
    schemaVersion: 1,
    build: { runtime: 'node', packages: ['.'], buildScript: null, startScript: 'start' },
    runtime: { port: 3000, healthPath: '/health' },
    storageBindings: [],
  },
  configurationSha256: 'b'.repeat(64),
  imageDigest: 'registry.example/app@sha256:' + 'c'.repeat(64),
  runtime: {
    runtime: 'node',
    version: '22.11.0',
    image: 'docker.io/library/node@sha256:' + 'd'.repeat(64),
    source: 'engines.node >=22 <23',
  },
  cleanupState: 'confirmed',
  requestedAt: new Date().toISOString(),
  acceptedAt: new Date().toISOString(),
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('deployment detail', () => {
  it('shows the runtime release the build used and why', async () => {
    vi.spyOn(api, 'app').mockImplementation(() => new Promise(() => {}));
    vi.spyOn(api, 'deployment').mockResolvedValue(deployment);
    vi.spyOn(api, 'log').mockResolvedValue({
      text: 'built\n',
      state: 'complete',
      nextOffset: 6,
      truncated: false,
    });
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <DeploymentPage id={deployment.applicationId} deployment={deployment.deploymentId} />
      </QueryClientProvider>,
    );
    expect(await screen.findByText('Node.js 22.11.0 · from engines.node >=22 <23')).toBeVisible();
  });

  it('names the platform default, a Bun packageManager pin, and older records', () => {
    const bun = {
      ...deployment.configuration,
      build: { ...deployment.configuration.build, runtime: 'bun' as const },
    };
    expect(
      deploymentRuntime({
        configuration: bun,
        runtime: {
          runtime: 'bun',
          version: '1.3.4',
          image: 'docker.io/oven/bun@sha256:' + 'e'.repeat(64),
          source: 'packageManager bun@1.3.4',
        },
      }),
    ).toBe('Bun 1.3.4 · from packageManager');
    expect(
      deploymentRuntime({
        configuration: deployment.configuration,
        runtime: {
          runtime: 'node',
          version: null,
          image: 'registry.example/node@sha256:' + 'f'.repeat(64),
          source: 'default',
        },
      }),
    ).toBe('Node.js (platform default)');
    // Built before runtimes were recorded, or not built yet.
    expect(deploymentRuntime({ configuration: bun, runtime: null })).toBe('Bun');
    expect(deploymentRuntime({ configuration: deployment.configuration })).toBe('Node.js');
  });
});
