import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { api, ApiError, configurationGuidance, validateSettings, type Settings } from './api';
import { Status } from './components/Status';
import { ConfigurationForm } from './pages/Configuration';

const settings: Settings = {
  revision: 1,
  repository: 'https://github.com/example/student-app',
  branch: 'main',
  configuration: {
    schemaVersion: 1,
    build: { runtime: 'node', packages: ['.'], buildScript: null, startScript: 'start' },
    runtime: { port: 3000, healthPath: '/health' },
    storageBindings: [],
  },
  configurationSha256: null,
};
describe('owner configuration', () => {
  it('accepts typed Node and Bun settings and rejects command text/path escapes', () => {
    expect(validateSettings(settings)).toBeNull();
    expect(
      validateSettings({
        ...settings,
        configuration: {
          ...settings.configuration,
          build: { ...settings.configuration.build, runtime: 'bun' },
        },
      }),
    ).toBeNull();
    for (const invalid of ['../escape', '/outside', 'a\\b'])
      expect(
        validateSettings({
          ...settings,
          configuration: {
            ...settings.configuration,
            build: { ...settings.configuration.build, packages: [invalid] },
          },
        }),
      ).not.toBeNull();
    expect(
      validateSettings({
        ...settings,
        configuration: {
          ...settings.configuration,
          build: { ...settings.configuration.build, startScript: 'node server.js' },
        },
      }),
    ).not.toBeNull();
    expect(
      validateSettings({
        ...settings,
        configuration: { ...settings.configuration, runtime: { port: 0, healthPath: '/health' } },
      }),
    ).not.toBeNull();
  });
  it('renders labeled editable controls and saves independently of deploy', async () => {
    const save = vi.spyOn(api, 'save').mockResolvedValue({ revision: 2 });
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ConfigurationForm id="app" initial={settings} />
      </QueryClientProvider>,
    );
    expect(screen.getByText(configurationGuidance.scripts)).toBeVisible();
    expect(screen.getByText(configurationGuidance.root, { exact: false })).toBeVisible();
    expect(screen.getByText(configurationGuidance.locks, { exact: false })).toBeVisible();
    expect(screen.getByText(configurationGuidance.health)).toBeVisible();
    fireEvent.click(screen.getByLabelText(/Bun/));
    fireEvent.change(screen.getByLabelText('Application port'), { target: { value: '8080' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save configuration' }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    expect(save.mock.calls[0][1].configuration.build.runtime).toBe('bun');
    expect(save.mock.calls[0][1].configuration.runtime.port).toBe(8080);
    expect(await screen.findByRole('status')).toHaveTextContent('Settings saved');
  });
  it('explains revision conflicts and preserves the draft', async () => {
    vi.spyOn(api, 'save').mockRejectedValue(
      new ApiError(409, 'REVISION_CONFLICT', 'Settings changed in another tab.'),
    );
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ConfigurationForm id="app" initial={settings} />
      </QueryClientProvider>,
    );
    fireEvent.change(screen.getByLabelText('Preferred branch'), {
      target: { value: 'feature/student' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save configuration' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Settings changed in another tab');
    expect(screen.getByLabelText('Preferred branch')).toHaveValue('feature/student');
  });
  it('reports statuses using text in addition to color', () => {
    render(
      <>
        <Status state="unknown" />
        <Status state="blocked" />
      </>,
    );
    expect(screen.getByText('Reconnecting')).toBeVisible();
    expect(screen.getByText('Recovery required')).toBeVisible();
  });
});
describe('typed API', () => {
  it('rejects malformed responses', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ data: { applicationId: 12 } }),
      }),
    );
    await expect(api.app('app')).rejects.toThrow('Invalid service response');
  });
  it('refreshes expired CSRF once while retaining the same mutation key', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: false,
        status: 403,
        json: async () => ({ error: { code: 'CSRF_REJECTED' } }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          data: {
            csrfToken: 'replacement',
            role: 'owner',
            stepUpExpiresAt: null,
            expiresAt: '2026-10-01T12:00:00Z',
            user: { id: 'u', displayName: 'Student', username: 'student' },
          },
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ data: { revision: 2 } }),
      });
    vi.stubGlobal('fetch', fetch);
    await api.save('app', settings, 'fixed-key');
    expect(fetch.mock.calls[0][1].headers['Idempotency-Key']).toBe('fixed-key');
    expect(fetch.mock.calls[2][1].headers['Idempotency-Key']).toBe('fixed-key');
    expect(fetch.mock.calls[2][1].headers['X-CSRF-Token']).toBe('replacement');
  });
});
