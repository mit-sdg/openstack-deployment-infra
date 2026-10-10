import {
  Alert,
  Button,
  Cluster,
  Dialog,
  ErrorAlert,
  Hint,
  IconButton,
  InlineStatus,
  Input,
  List,
  ListItem,
  LoadingRows,
  RelativeTime,
  Section,
} from '@openstack-platform/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import {
  api,
  configurationGuidance,
  resourceApi,
  validateBindings,
  type StorageBinding,
  type StorageResource,
} from '../api';
import { QueryError } from './Feedback';
import { Operation, OperationList } from './Operation';
import { StorageUsage, StorageLimitsControl } from './StorageUsage';
import { Status } from './Status';
import '../pages/app-pages.css';

const labels = { postgres: 'PostgreSQL', mongo: 'MongoDB', s3: 'S3 storage' };
const types = ['postgres', 'mongo', 's3'] as const;
const finished = ['succeeded', 'failed', 'blocked'];

type Request = {
  type?: StorageResource['type'];
  resource?: string;
  action?: 'rotate' | 'verify';
};

function sentence(value: string) {
  return value.charAt(0).toUpperCase() + value.slice(1).replaceAll('_', ' ');
}

/** Editor for the environment variable names one resource sets. */
function BindingsDialog({
  resource,
  outputs,
  validate,
  onApply,
  onClose,
}: {
  resource: StorageResource;
  outputs: Record<string, string>;
  validate: (outputs: Record<string, string>) => string | null;
  /** Applies the names; may save them right away (a rejected promise keeps the dialog open). */
  onApply: (outputs: Record<string, string>) => void | Promise<unknown>;
  onClose: () => void;
}) {
  // A resource without variables starts from the defaults: most apps want them.
  const [draft, setDraft] = useState<Record<string, string>>(() => ({
    ...(Object.keys(outputs).length ? outputs : resource.defaultBindings),
  }));
  const [error, setError] = useState<unknown>(null);
  const [saving, setSaving] = useState(false);
  const certificates = Object.values(resource.defaultBindings).filter((name) =>
    /SSLROOTCERT/.test(name),
  );
  function set(name: string, target: string | null) {
    setError(null);
    setDraft((current) => {
      const next = { ...current };
      if (target === null) delete next[name];
      else next[name] = target;
      return next;
    });
  }
  return (
    <Dialog
      open
      onClose={onClose}
      title={`${labels[resource.type]} variables`}
      footer={
        <>
          <Button
            variant="ghost"
            onClick={() => {
              setError(null);
              setDraft({ ...resource.defaultBindings });
            }}
          >
            Reset to defaults
          </Button>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={saving}
            onClick={async () => {
              const invalid = validate(draft);
              setError(invalid);
              if (invalid) return;
              setSaving(true);
              try {
                await onApply(draft);
              } catch (failure) {
                setError(failure);
              } finally {
                setSaving(false);
              }
            }}
          >
            Save variables
          </Button>
        </>
      }
    >
      <Hint>
        Choose the environment variable name your app reads for each value. Remove the ones your app
        doesn’t use.
      </Hint>
      <div role="group" aria-label={`${labels[resource.type]} bindings`} className="app-bindings">
        {Object.entries(resource.defaultBindings).map(([name, defaultTarget]) => (
          <div className="app-binding" key={name}>
            <code className="app-binding__name">{name}</code>
            {name in draft ? (
              <>
                <Input
                  id={`${resource.resourceId}-${name}`}
                  aria-label={`${name} → environment name`}
                  className="app-mono-input"
                  value={draft[name]}
                  autoComplete="off"
                  autoCapitalize="characters"
                  spellCheck={false}
                  onChange={(event) => set(name, event.target.value)}
                />
                <IconButton
                  icon="x"
                  label={`Remove ${name}`}
                  className="ui-icon-button--ghost"
                  onClick={() => set(name, null)}
                />
              </>
            ) : (
              <>
                <span className="ui-text-subtle">Not set</span>
                <Button
                  size="sm"
                  variant="ghost"
                  icon="plus"
                  aria-label={`Add ${name} as ${defaultTarget}`}
                  onClick={() => set(name, defaultTarget)}
                >
                  Add
                </Button>
              </>
            )}
          </div>
        ))}
      </div>
      {!!certificates.length && (
        <Hint>
          Keep {certificates.join(' and ')} so your app can verify the TLS certificate when it
          connects.
        </Hint>
      )}
      {'public_endpoint' in resource.defaultBindings && (
        <Hint>
          Use the private endpoint from your server. Sign browser upload and download links with the
          public endpoint; browsers on your app’s pages can use them directly.
        </Hint>
      )}
      <ErrorAlert error={error} />
    </Dialog>
  );
}

export function StorageSection({
  id,
  bindings,
  onChange,
  service = api,
  notice,
  admin = false,
  save,
  identityProvider: providedIdentityProvider = false,
}: {
  id: string;
  admin?: boolean;
  service?: ReturnType<typeof resourceApi>;
  /**
   * Show sign-in information with storage changes.
   * Owners read the sign-in flag themselves.
   */
  identityProvider?: boolean;
  bindings: StorageBinding[];
  onChange: (bindings: StorageBinding[]) => void;
  /**
   * Saves new bindings straight away. Without it, binding changes only
   * update the draft through onChange and the parent saves them.
   */
  save?: (bindings: StorageBinding[]) => Promise<unknown>;
  /** Optional message shown at the top of the section, e.g. unsaved changes. */
  notice?: ReactNode;
}) {
  const injected = service !== api;
  const client = useQueryClient();
  const storage = useQuery({
    queryKey: ['storage', id],
    queryFn: () => service.storage(id),
    refetchInterval: 1500,
    refetchOnWindowFocus: true,
  });
  const environment = useQuery({
    queryKey: ['environment', id],
    queryFn: () => service.environment(id),
  });
  // Show the sign-in dependency in the dialog. Owners read the flag here;
  // callers may pass it in.
  const app = useQuery({
    queryKey: ['app', id],
    queryFn: () => api.app(id),
    enabled: !injected,
  });
  const identityProvider = providedIdentityProvider || app.data?.identityProvider === true;
  const owner = app.data?.access !== 'admin';
  const busy =
    storage.data?.intents.some((intent) => !['succeeded', 'failed'].includes(intent.state)) ||
    environment.data?.intents?.some((intent) => !['succeeded', 'failed'].includes(intent.state));
  const [createdType, setCreatedType] = useState<StorageResource['type'] | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  // Right after adding a resource, open its variables prefilled with defaults.
  useEffect(() => {
    const created = storage.data?.items.find((resource) => resource.type === createdType);
    if (created) {
      setEditing(created.resourceId);
      setCreatedType(null);
    }
  }, [storage.data, createdType]);
  const [confirming, setConfirming] = useState<Request | null>(null);
  const [applying, setApplying] = useState<string | null>(null);
  const [applyError, setApplyError] = useState<unknown>(null);
  const [started, setStarted] = useState<string[]>([]);
  const pending = useRef<{ subject: string; key: string } | null>(null);
  const action = useMutation({
    mutationFn: ({ type, resource, action }: Request) => {
      const subject = type ?? `${resource}/${action}`;
      const key = pending.current?.subject === subject ? pending.current.key : crypto.randomUUID();
      pending.current = { subject, key };
      return type
        ? service.createStorage(id, type, key)
        : service.storageAction(id, resource!, action!, key);
    },
    onSuccess: (result, variables) => {
      if (variables.type) setCreatedType(variables.type);
      pending.current = null;
      setStarted((current) => [...current, result.intentId]);
      client.invalidateQueries({ queryKey: ['storage', id] });
      client.invalidateQueries({ queryKey: ['intents'] });
    },
  });
  // Owners confirm adding storage because deletion needs staff or an admin.
  function request(next: Request) {
    if (next.action === 'rotate' || identityProvider || (owner && next.type)) {
      setConfirming(next);
    } else action.mutate(next);
  }
  const names = environment.data?.items.map((item) => item.name) ?? [];
  const invalid = validateBindings(bindings, names);
  function withOutputs(resource: string, outputs: Record<string, string>) {
    return [
      ...bindings.filter((item) => item.resourceId !== resource),
      ...(Object.keys(outputs).length ? [{ resourceId: resource, outputs }] : []),
    ];
  }
  function apply(resource: string, outputs: Record<string, string>) {
    const next = withOutputs(resource, outputs);
    if (save) return save(next);
    onChange(next);
  }
  async function applyDefaults(resource: StorageResource) {
    setApplyError(null);
    // Names that clash with existing variables need a choice: open the editor.
    if (validateBindings(withOutputs(resource.resourceId, resource.defaultBindings), names)) {
      setEditing(resource.resourceId);
      return;
    }
    setApplying(resource.resourceId);
    try {
      await apply(resource.resourceId, { ...resource.defaultBindings });
    } catch (failure) {
      setApplyError(failure);
    } finally {
      setApplying(null);
    }
  }
  const editingResource = storage.data?.items.find((item) => item.resourceId === editing);
  const confirmingResource = storage.data?.items.find(
    (item) => item.resourceId === confirming?.resource,
  );
  const confirmingLabel = labels[confirming?.type ?? confirmingResource?.type ?? 'postgres'];
  const recent =
    storage.data?.intents.filter(
      (intent) => started.includes(intent.intentId) || !finished.includes(intent.state),
    ) ?? [];
  return (
    <Section title="Databases and storage" flush>
      {notice && <div className="app-block">{notice}</div>}
      {(!!action.error || !!applyError) && (
        <div className="app-block">
          <ErrorAlert error={action.error ?? applyError} />
        </div>
      )}
      {storage.isPending ? (
        <LoadingRows rows={3} />
      ) : storage.error ? (
        <div className="app-block">
          <QueryError query={storage} what="your databases and storage" />
        </div>
      ) : (
        <div className="app-resources">
          <List label="Databases and storage">
            {types.map((type) => {
              const resource = storage.data?.items.find((item) => item.type === type);
              if (!resource)
                return (
                  <ListItem
                    key={type}
                    title={labels[type]}
                    meta={<span>Not added</span>}
                    trailing={
                      <Button
                        size="sm"
                        icon="plus"
                        disabled={
                          storage.isPending ||
                          !!storage.error ||
                          action.isPending ||
                          busy ||
                          storage.data?.intents.some(
                            (intent) =>
                              intent.kind === 'storage_create' &&
                              intent.type === type &&
                              !['succeeded', 'failed'].includes(intent.state),
                          )
                        }
                        loading={action.isPending && action.variables?.type === type}
                        onClick={() => request({ type })}
                      >
                        Add {labels[type]}
                      </Button>
                    }
                  />
                );
              const outputs =
                bindings.find((item) => item.resourceId === resource.resourceId)?.outputs ?? {};
              const count = Object.keys(outputs).length;
              const ready = resource.status === 'ready';
              const disabled = action.isPending || !!busy || !ready;
              return (
                <ListItem
                  key={type}
                  title={
                    <span className="app-inline">
                      {labels[type]}
                      <Status
                        state={resource.status === 'provisioning' ? 'creating' : resource.status}
                        label={
                          resource.status === 'provisioning'
                            ? 'Setting up'
                            : ready
                              ? undefined
                              : sentence(resource.status)
                        }
                      />
                    </span>
                  }
                  trailing={
                    <>
                      {!!count && (
                        <Button
                          size="sm"
                          aria-label={`Edit ${labels[type]} variables`}
                          onClick={() => setEditing(resource.resourceId)}
                        >
                          Edit variables
                        </Button>
                      )}
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={`Verify ${labels[type]}`}
                        disabled={disabled}
                        loading={
                          action.isPending &&
                          action.variables?.resource === resource.resourceId &&
                          action.variables?.action === 'verify'
                        }
                        onClick={() => request({ resource: resource.resourceId, action: 'verify' })}
                      >
                        Verify
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={`Rotate ${labels[type]} credentials`}
                        disabled={disabled}
                        loading={
                          action.isPending &&
                          action.variables?.resource === resource.resourceId &&
                          action.variables?.action === 'rotate'
                        }
                        onClick={() => request({ resource: resource.resourceId, action: 'rotate' })}
                      >
                        Rotate credentials
                      </Button>
                    </>
                  }
                  meta={
                    <>
                      {!!count && (
                        <span>{`${count} ${count === 1 ? 'variable' : 'variables'}`}</span>
                      )}
                      {resource.verifiedAt ? (
                        <span>
                          Verified <RelativeTime value={resource.verifiedAt} />
                        </span>
                      ) : (
                        <span>
                          Added <RelativeTime value={resource.createdAt} />
                        </span>
                      )}
                    </>
                  }
                >
                  <StorageUsage resource={resource} />
                  {admin && (
                    <StorageLimitsControl
                      id={id}
                      resource={resource}
                      service={service}
                      disabled={disabled}
                    />
                  )}
                  {!count && (
                    <>
                      <InlineStatus tone="warning">
                        Your app can’t connect to {labels[type]} yet. Give it the connection
                        variables.
                      </InlineStatus>
                      <Cluster className="app-resource-actions">
                        <Button
                          size="sm"
                          loading={applying === resource.resourceId}
                          aria-label={`Use default ${labels[type]} variables`}
                          onClick={() => applyDefaults(resource)}
                        >
                          Use default variables
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          aria-label={`Choose ${labels[type]} variable names`}
                          onClick={() => setEditing(resource.resourceId)}
                        >
                          Choose names
                        </Button>
                      </Cluster>
                    </>
                  )}
                </ListItem>
              );
            })}
          </List>
        </div>
      )}
      {invalid && (
        <div className="app-block">
          <ErrorAlert error={invalid} focus={false} />
        </div>
      )}
      {!!recent.length && (
        <div className="app-divider">
          <OperationList label="Storage changes">
            {recent.slice(0, 6).map((intent) => (
              <Operation key={intent.intentId} intent={intent} showApp={false} />
            ))}
          </OperationList>
        </div>
      )}
      <p className="app-block app-block--subtle ui-hint">
        Databases are backed up every night. {configurationGuidance.postgres} Staff or an admin can
        delete a database or storage.
      </p>
      {editingResource && (
        <BindingsDialog
          key={editingResource.resourceId}
          resource={editingResource}
          outputs={
            bindings.find((item) => item.resourceId === editingResource.resourceId)?.outputs ?? {}
          }
          validate={(outputs) =>
            validateBindings(
              [
                ...bindings.filter((item) => item.resourceId !== editingResource.resourceId),
                { resourceId: editingResource.resourceId, outputs },
              ],
              names,
            )
          }
          onApply={async (outputs) => {
            await apply(editingResource.resourceId, outputs);
            setEditing(null);
          }}
          onClose={() => setEditing(null)}
        />
      )}
      <Dialog
        open={confirming !== null}
        onClose={() => setConfirming(null)}
        size="sm"
        title={
          confirming?.action === 'rotate'
            ? `Rotate ${confirmingLabel} credentials?`
            : confirming?.action === 'verify'
              ? `Verify ${confirmingLabel}?`
              : `Add ${confirmingLabel}?`
        }
        footer={
          <>
            <Button onClick={() => setConfirming(null)}>Cancel</Button>
            <Button
              variant="primary"
              onClick={() => {
                if (confirming) action.mutate(confirming);
                setConfirming(null);
              }}
            >
              {confirming?.action === 'rotate'
                ? 'Rotate credentials'
                : confirming?.action === 'verify'
                  ? 'Verify'
                  : `Add ${confirmingLabel}`}
            </Button>
          </>
        }
      >
        {confirming?.type && (
          <p>
            This adds {confirmingLabel} to this app. Staff or an admin can delete it later, so add
            it only if your app needs it.
          </p>
        )}
        {confirming?.action === 'rotate' && (
          <p>
            {confirmingLabel} gets new credentials. Your app picks them up on its next deploy, so
            deploy again after rotating.
          </p>
        )}
        {identityProvider && (
          <Alert tone="info">Signing in to this portal depends on this app.</Alert>
        )}
      </Dialog>
    </Section>
  );
}
