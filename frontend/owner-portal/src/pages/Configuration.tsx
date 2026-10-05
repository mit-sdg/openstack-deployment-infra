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
  PageSkeleton,
  Radio,
  Section,
  SectionSkeleton,
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
import { QueryError } from '../components/Feedback';
import { RepositoryAccess } from '../components/RepositoryAccess';
import { StorageSection } from '../components/StorageSection';
import './app-pages.css';

export function ConfigurationPage({ id }: { id: string }) {
  const query = useQuery({ queryKey: ['settings', id], queryFn: () => api.settings(id) });
  return (
    <AppFrame id={id} active="Settings">
      {query.isPending ? (
        <PageSkeleton label="Loading settings…">
          <SectionSkeleton rows={8} />
          <SectionSkeleton title variant="list" rows={2} />
          <SectionSkeleton title variant="list" rows={3} />
        </PageSkeleton>
      ) : query.error ? (
        <QueryError query={query} what="your settings" />
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
  identityProvider = false,
}: {
  id: string;
  initial: Settings;
  resources?: boolean;
  service?: ReturnType<typeof resourceApi>;
  /** Admin view of the sign-in app: storage changes need explicit consent. */
  identityProvider?: boolean;
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
  // The last saved settings, so variable names can be saved on their own
  // without also saving unrelated edits still in the form.
  const [savedSettings, setSavedSettings] = useState<Settings>(() => structuredClone(initial));
  const submitted = useRef<Settings>(initial);
  const client = useQueryClient();
  function refresh() {
    client.invalidateQueries({ queryKey: [...scope, 'settings', id] });
    client.invalidateQueries({ queryKey: [...scope, 'app', id] });
  }
  const save = useMutation({
    mutationFn: (key: string) => {
      submitted.current = structuredClone(settings);
      return service.save(id, settings, key);
    },
    onSuccess: (result) => {
      const revision = result.revision as number;
      setSettings((current) => ({ ...current, revision }));
      setSavedSettings({ ...submitted.current, revision });
      setSaved(true);
      setSavedBindings(bindingKey(submitted.current.configuration.storageBindings));
      refresh();
    },
  });
  const saveBindings = useMutation({
    mutationFn: ({ bindings, key }: { bindings: StorageBinding[]; key: string }) =>
      service.save(
        id,
        {
          ...savedSettings,
          revision: settings.revision,
          configuration: { ...savedSettings.configuration, storageBindings: bindings },
        },
        key,
      ),
    onSuccess: (result, { bindings }) => {
      const revision = result.revision as number;
      setSettings((current) => ({
        ...current,
        revision,
        configuration: { ...current.configuration, storageBindings: bindings },
      }));
      setSavedSettings((current) => ({
        ...current,
        revision,
        configuration: { ...current.configuration, storageBindings: bindings },
      }));
      setSavedBindings(bindingKey(bindings));
      refresh();
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
            <Hint>{configurationGuidance.versions}</Hint>
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
          <RepositoryAccess id={id} service={service} saved={savedSettings.revision > 0} />
          <EnvironmentSection
            service={service}
            id={id}
            bindings={settings.configuration.storageBindings}
          />
          <StorageSection
            service={service}
            identityProvider={identityProvider}
            id={id}
            bindings={settings.configuration.storageBindings}
            onChange={(storageBindings) =>
              update({ configuration: { ...settings.configuration, storageBindings } })
            }
            // Before the first save there are no settings to add names to, so
            // names stay in the draft and are saved with the form.
            save={
              settings.revision
                ? (bindings) => saveBindings.mutateAsync({ bindings, key: crypto.randomUUID() })
                : undefined
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
