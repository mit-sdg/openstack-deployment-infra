import {
  Alert,
  Button,
  ErrorAlert,
  Field,
  Fieldset,
  Grid,
  Hint,
  InlineStatus,
  Input,
  LoadingRows,
  Radio,
  Section,
  Textarea,
} from '@openstack-platform/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useId, useRef, useState, type ReactNode } from 'react';
import {
  api,
  configurationGuidance,
  resourceApi,
  validateSettings,
  validateBindings,
  type Settings,
  type StorageBinding,
} from '../api';
import { AppFrame } from '../components/AppFrame';
import { EnvironmentSection } from '../components/EnvironmentSection';
import { StorageSection } from '../components/StorageSection';
import './app-pages.css';

export function ConfigurationPage({ id }: { id: string }) {
  const query = useQuery({ queryKey: ['settings', id], queryFn: () => api.settings(id) });
  return (
    <AppFrame id={id} active="Settings">
      {query.isPending ? (
        <Section aria-label="Settings">
          <LoadingRows rows={4} />
        </Section>
      ) : query.error ? (
        <ErrorAlert error={query.error} />
      ) : (
        <ConfigurationForm key={id} id={id} initial={query.data} resources />
      )}
    </AppFrame>
  );
}

/** One titled group of fields inside the settings card. */
function Group({ title, children }: { title: string; children: ReactNode }) {
  const id = useId();
  return (
    <div className="app-settings-group" role="group" aria-labelledby={id}>
      <h3 id={id} className="app-settings-group__title">
        {title}
      </h3>
      <div className="ui-stack ui-gap-4">{children}</div>
    </div>
  );
}

/** Stable comparison of bindings, independent of order. */
function bindingKey(bindings: StorageBinding[]) {
  return JSON.stringify(
    [...bindings]
      .sort((a, b) => a.resourceId.localeCompare(b.resourceId))
      .map((binding) => [binding.resourceId, Object.entries(binding.outputs).sort()]),
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
  const formId = useId();
  const environment = useQuery({
    queryKey: [...scope, 'environment', id],
    queryFn: () => service.environment(id),
    enabled: resources,
  });
  const [settings, setSettings] = useState<Settings>(structuredClone(initial));
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [savedBindings, setSavedBindings] = useState(() =>
    bindingKey(initial.configuration.storageBindings),
  );
  const submitted = useRef('');
  const client = useQueryClient();
  const save = useMutation({
    mutationFn: (key: string) => {
      submitted.current = bindingKey(settings.configuration.storageBindings);
      return service.save(id, settings, key);
    },
    onSuccess: (result) => {
      setSettings((current) => ({ ...current, revision: result.revision as number }));
      setSaved(true);
      setSavedBindings(submitted.current);
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
  const unsavedBindings = bindingKey(settings.configuration.storageBindings) !== savedBindings;
  return (
    <>
      <form
        id={formId}
        noValidate
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
        <Section
          flush
          aria-label="Build and run settings"
          footer={
            <>
              {saved && (
                <InlineStatus>Settings saved. They apply on your next deploy.</InlineStatus>
              )}
              <Button type="submit" variant="primary" loading={save.isPending}>
                Save settings
              </Button>
            </>
          }
        >
          <Group title="Source">
            <Field label="Repository URL" id="repository" hint={configurationGuidance.root}>
              <Input
                type="url"
                placeholder="https://github.com/your-name/your-app"
                value={settings.repository}
                onChange={(e) => update({ repository: e.target.value })}
                required
              />
            </Field>
            <Field
              label="Branch"
              id="branch"
              hint="Only a label. You choose the exact commit when you deploy."
            >
              <Input
                value={settings.branch}
                onChange={(e) => update({ branch: e.target.value })}
                required
              />
            </Field>
          </Group>
          <Group title="Build">
            <Fieldset legend="Runtime" variant="cards">
              {(['node', 'bun'] as const).map((name) => (
                <Radio
                  key={name}
                  name="runtime"
                  value={name}
                  label={name === 'node' ? 'Node.js' : 'Bun'}
                  description={
                    name === 'node' ? 'Uses package-lock.json' : 'Uses bun.lock or bun.lockb'
                  }
                  checked={settings.configuration.build.runtime === name}
                  onChange={() => build({ runtime: name })}
                />
              ))}
            </Fieldset>
            <Field
              label="Package directories"
              id="packages"
              hint="One per line. Use . for the repository root. Each needs its lockfile."
            >
              <Textarea
                rows={2}
                value={settings.configuration.build.packages.join('\n')}
                onChange={(e) => build({ packages: e.target.value.split('\n') })}
              />
            </Field>
            <Grid columns={2}>
              <Field label="Build script" id="build-script" optional>
                <Input
                  placeholder="build"
                  value={settings.configuration.build.buildScript ?? ''}
                  onChange={(e) => build({ buildScript: e.target.value || null })}
                />
              </Field>
              <Field label="Start script" id="start-script">
                <Input
                  value={settings.configuration.build.startScript}
                  onChange={(e) => build({ startScript: e.target.value })}
                  required
                />
              </Field>
            </Grid>
            <Hint>{configurationGuidance.scripts}</Hint>
          </Group>
          <Group title="Runtime">
            <Grid columns={2}>
              <Field label="Port" id="port">
                <Input
                  type="number"
                  inputMode="numeric"
                  min={1}
                  max={65535}
                  value={settings.configuration.runtime.port}
                  onChange={(e) => runtime({ port: Number(e.target.value) })}
                  required
                />
              </Field>
              <Field label="Health check path" id="health">
                <Input
                  value={settings.configuration.runtime.healthPath}
                  onChange={(e) => runtime({ healthPath: e.target.value })}
                  required
                />
              </Field>
            </Grid>
            <Hint>{configurationGuidance.health}</Hint>
          </Group>
          {(error || save.error) && (
            <div className="app-settings-group">
              <ErrorAlert error={error ?? save.error} />
            </div>
          )}
        </Section>
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
            notice={
              unsavedBindings && (
                <Alert
                  tone="warning"
                  action={
                    <Button type="submit" form={formId} size="sm" loading={save.isPending}>
                      Save changes
                    </Button>
                  }
                >
                  Save your settings to keep these variable changes. They apply on your next deploy.
                </Alert>
              )
            }
          />
        </>
      )}
    </>
  );
}
