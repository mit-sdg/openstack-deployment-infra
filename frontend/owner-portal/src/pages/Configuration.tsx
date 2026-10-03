import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import {
  api,
  configurationGuidance,
  resourceApi,
  validateSettings,
  validateBindings,
  type Settings,
} from '../api';
import { AppFrame } from '../components/AppFrame';
import { EnvironmentSection } from '../components/EnvironmentSection';
import { StorageSection } from '../components/StorageSection';
import { ErrorNotice, Loading } from '../components/Feedback';

export function ConfigurationPage({ id }: { id: string }) {
  const query = useQuery({ queryKey: ['settings', id], queryFn: () => api.settings(id) });
  return (
    <AppFrame id={id} active="Configuration">
      {query.isPending ? (
        <Loading />
      ) : query.error ? (
        <ErrorNotice error={query.error} />
      ) : (
        <ConfigurationForm key={id} id={id} initial={query.data!} resources />
      )}
    </AppFrame>
  );
}

export function ConfigurationForm({
  id,
  initial,
  resources = false,
  service = api,
}: {
  id: string;
  initial: Settings;
  resources?: boolean;
  service?: ReturnType<typeof resourceApi>;
}) {
  const scope = service === api ? [] : ['admin'];
  const environment = useQuery({
    queryKey: [...scope, 'environment', id],
    queryFn: () => service.environment(id),
    enabled: resources,
  });
  const [settings, setSettings] = useState<Settings>(structuredClone(initial));
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const client = useQueryClient();
  const save = useMutation({
    mutationFn: (key: string) => service.save(id, settings, key),
    onSuccess: (result) => {
      setSettings((current) => ({ ...current, revision: result.revision as number }));
      setSaved(true);
      client.invalidateQueries({ queryKey: [...scope, 'settings', id] });
      client.invalidateQueries({ queryKey: [...scope, 'app', id] });
    },
  });
  function update(change: Partial<Settings>) {
    setSaved(false);
    setSettings((current) => ({ ...current, ...change }));
  }
  function build(change: Partial<Settings['configuration']['build']>) {
    update({
      configuration: {
        ...settings.configuration,
        build: { ...settings.configuration.build, ...change },
      },
    });
  }
  function runtime(change: Partial<Settings['configuration']['runtime']>) {
    update({
      configuration: {
        ...settings.configuration,
        runtime: { ...settings.configuration.runtime, ...change },
      },
    });
  }
  return (
    <>
      <div className="section-heading">
        <div>
          <h2>Application configuration</h2>
          <p>Save your settings independently from deploying your code.</p>
        </div>
        <span className="chip">Revision {settings.revision}</span>
      </div>
      <form
        className="card form-card"
        onSubmit={(event) => {
          event.preventDefault();
          const validation =
            validateSettings(settings) ??
            validateBindings(
              settings.configuration.storageBindings,
              environment.data?.items.map((item) => item.name) ?? [],
            );
          setError(validation);
          if (!validation) save.mutate(crypto.randomUUID());
        }}
      >
        <section className="form-section">
          <div className="form-section-heading">
            <span className="step-number">01</span>
            <div>
              <h3>Source</h3>
              <p>Start with a public GitHub repository.</p>
            </div>
          </div>
          <div className="fields-grid">
            <div className="field full">
              <label htmlFor="repository">Repository URL</label>
              <input
                id="repository"
                type="url"
                placeholder="https://github.com/your-name/your-app"
                value={settings.repository}
                onChange={(e) => update({ repository: e.target.value })}
                required
              />
              <p className="field-help">
                Public, credential-free repositories only. {configurationGuidance.root}
              </p>
            </div>
            <div className="field">
              <label htmlFor="branch">Preferred branch</label>
              <input
                id="branch"
                value={settings.branch}
                onChange={(e) => update({ branch: e.target.value })}
                required
              />
              <p className="field-help">
                A label for your deployment. You choose the exact commit separately.
              </p>
            </div>
          </div>
        </section>
        <section className="form-section">
          <div className="form-section-heading">
            <span className="step-number">02</span>
            <div>
              <h3>Build</h3>
              <p>{configurationGuidance.scripts}</p>
            </div>
          </div>
          <div className="fields-grid">
            <fieldset className="runtime-choice full">
              <legend>JavaScript runtime</legend>
              {(['node', 'bun'] as const).map((name) => (
                <label
                  key={name}
                  className={`runtime-option ${settings.configuration.build.runtime === name ? 'selected' : ''}`}
                >
                  <input
                    type="radio"
                    name="runtime"
                    value={name}
                    checked={settings.configuration.build.runtime === name}
                    onChange={() => build({ runtime: name })}
                  />
                  <span>
                    <strong>{name === 'node' ? 'Node.js' : 'Bun'}</strong>
                    <small>
                      {name === 'node' ? 'Uses package-lock.json' : 'Uses bun.lock or bun.lockb'}
                    </small>
                  </span>
                </label>
              ))}
            </fieldset>
            <div className="field full">
              <label htmlFor="packages">Package directories</label>
              <textarea
                id="packages"
                rows={2}
                value={settings.configuration.build.packages.join('\n')}
                onChange={(e) => build({ packages: e.target.value.split('\n') })}
              />
              <p className="field-help">
                One directory per line. Use . for the repository root. {configurationGuidance.locks}
              </p>
            </div>
            <div className="field">
              <label htmlFor="build-script">
                Build script <span className="optional">optional</span>
              </label>
              <input
                id="build-script"
                placeholder="build"
                value={settings.configuration.build.buildScript ?? ''}
                onChange={(e) => build({ buildScript: e.target.value || null })}
              />
              <p className="field-help">Leave empty if your app needs no build step.</p>
            </div>
            <div className="field">
              <label htmlFor="start-script">Start script</label>
              <input
                id="start-script"
                value={settings.configuration.build.startScript}
                onChange={(e) => build({ startScript: e.target.value })}
                required
              />
              <p className="field-help">A script name from the root package.json, such as start.</p>
            </div>
          </div>
        </section>
        <section className="form-section">
          <div className="form-section-heading">
            <span className="step-number">03</span>
            <div>
              <h3>Runtime</h3>
              <p>Tell the platform where to reach your app and check its health.</p>
            </div>
          </div>
          <div className="fields-grid">
            <div className="field">
              <label htmlFor="port">Application port</label>
              <input
                id="port"
                type="number"
                min={1}
                max={65535}
                value={settings.configuration.runtime.port}
                onChange={(e) => runtime({ port: Number(e.target.value) })}
                required
              />
            </div>
            <div className="field">
              <label htmlFor="health">Health path</label>
              <input
                id="health"
                value={settings.configuration.runtime.healthPath}
                onChange={(e) => runtime({ healthPath: e.target.value })}
                required
              />
              <p className="field-help">{configurationGuidance.health}</p>
            </div>
          </div>
          <ErrorNotice error={error ?? save.error} />
          {saved && (
            <p className="saved-message" role="status">
              Settings saved. They apply to your next deployment.
            </p>
          )}
        </section>
        <div className="form-footer">
          <p className="muted">Saving does not deploy or restart your application.</p>
          <button className="button button-primary" disabled={save.isPending}>
            {save.isPending ? 'Saving…' : 'Save configuration'}
          </button>
        </div>
      </form>
      {resources && (
        <>
          <EnvironmentSection
            service={service}
            id={id}
            bindings={settings.configuration.storageBindings}
          />
          <StorageSection
            service={service}
            id={id}
            bindings={settings.configuration.storageBindings}
            onChange={(storageBindings) =>
              update({ configuration: { ...settings.configuration, storageBindings } })
            }
          />
        </>
      )}
    </>
  );
}
