import {
  Button,
  Checkbox,
  Cluster,
  Dialog,
  ErrorAlert,
  Hint,
  IconButton,
  Input,
  List,
  ListItem,
  LoadingRows,
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
import { relativeTime } from '../utils/presentation';
import { Operation, OperationList } from './Operation';
import { Status } from './Status';
import '../pages/app-pages.css';

const labels = { postgres: 'PostgreSQL', mongo: 'MongoDB', s3: 'S3 storage' };
const types = ['postgres', 'mongo', 's3'] as const;
const finished = ['succeeded', 'failed', 'blocked'];

type Request = {
  type?: StorageResource['type'];
  resource?: string;
  action?: 'rotate' | 'verify';
  /** The person confirmed that portal sign-in depends on this app. */
  identity?: boolean;
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
  onApply: (outputs: Record<string, string>) => void;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState<Record<string, string>>(() => ({ ...outputs }));
  const [error, setError] = useState<string | null>(null);
  const certificates = Object.values(resource.defaultBindings).filter((name) =>
    /SSLROOTCERT|CA_BUNDLE/.test(name),
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
            onClick={() => {
              const invalid = validate(draft);
              setError(invalid);
              if (!invalid) onApply(draft);
            }}
          >
            Apply
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
}: {
  id: string;
  service?: ReturnType<typeof resourceApi>;
  bindings: StorageBinding[];
  onChange: (bindings: StorageBinding[]) => void;
  /** Optional message shown at the top of the section, e.g. unsaved changes. */
  notice?: ReactNode;
}) {
  const owner = service === api;
  const scope = owner ? [] : ['admin'];
  const client = useQueryClient();
  const storage = useQuery({
    queryKey: [...scope, 'storage', id],
    queryFn: () => service.storage(id),
    refetchInterval: owner ? 1500 : 5000,
    refetchOnWindowFocus: owner,
  });
  const environment = useQuery({
    queryKey: [...scope, 'environment', id],
    queryFn: () => service.environment(id),
  });
  // Owners confirm identity-provider changes in a dialog rather than the
  // request layer's native prompt. Admin services bring their own confirmation.
  const app = useQuery({
    queryKey: ['app', id],
    queryFn: () => api.app(id),
    enabled: owner,
  });
  const identityProvider = owner && app.data?.identityProvider === true;
  const busy =
    storage.data?.intents.some((intent) => !['succeeded', 'failed'].includes(intent.state)) ||
    environment.data?.intents?.some((intent) => !['succeeded', 'failed'].includes(intent.state));
  const [createdType, setCreatedType] = useState<StorageResource['type'] | null>(null);
  useEffect(() => {
    const created = storage.data?.items.find((resource) => resource.type === createdType);
    if (created) {
      onChange([
        ...bindings.filter((item) => item.resourceId !== created.resourceId),
        { resourceId: created.resourceId, outputs: { ...created.defaultBindings } },
      ]);
      setCreatedType(null);
    }
  }, [storage.data, createdType, bindings, onChange]);
  const [confirming, setConfirming] = useState<Request | null>(null);
  const [identityConfirmed, setIdentityConfirmed] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [started, setStarted] = useState<string[]>([]);
  const pending = useRef<{ subject: string; key: string } | null>(null);
  const action = useMutation({
    mutationFn: ({ type, resource, action, identity }: Request) => {
      const subject = type ?? `${resource}/${action}`;
      const key = pending.current?.subject === subject ? pending.current.key : crypto.randomUUID();
      pending.current = { subject, key };
      const target = owner && identity ? resourceApi('/apps', () => true) : service;
      return type
        ? target.createStorage(id, type, key)
        : target.storageAction(id, resource!, action!, key);
    },
    onSuccess: (result, variables) => {
      if (variables.type) setCreatedType(variables.type);
      pending.current = null;
      setStarted((current) => [...current, result.intentId]);
      client.invalidateQueries({ queryKey: [...scope, 'storage', id] });
      client.invalidateQueries({ queryKey: ['intents'] });
    },
  });
  function request(next: Request) {
    if (next.action === 'rotate' || identityProvider) {
      setIdentityConfirmed(false);
      setConfirming(next);
    } else action.mutate(next);
  }
  const names = environment.data?.items.map((item) => item.name) ?? [];
  const invalid = validateBindings(bindings, names);
  function replace(resource: string, outputs: Record<string, string>) {
    onChange([
      ...bindings.filter((item) => item.resourceId !== resource),
      ...(Object.keys(outputs).length ? [{ resourceId: resource, outputs }] : []),
    ]);
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
      {(storage.error || action.error) && (
        <div className="app-block">
          <ErrorAlert error={storage.error ?? action.error} />
        </div>
      )}
      {storage.isPending ? (
        <LoadingRows rows={3} />
      ) : (
        <List label="Databases and storage">
          {types.map((type) => {
            const resource = storage.data?.items.find((item) => item.type === type);
            if (!resource)
              return (
                <ListItem key={type} title={labels[type]} meta={<span>Not added</span>}>
                  <Cluster className="app-resource-actions">
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
                      onClick={() => request({ type })}
                    >
                      Add {labels[type]}
                    </Button>
                  </Cluster>
                </ListItem>
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
                meta={
                  <>
                    <span>
                      {count
                        ? `${count} ${count === 1 ? 'variable' : 'variables'}`
                        : 'No variables'}
                    </span>
                    {resource.verifiedAt ? (
                      <time
                        dateTime={resource.verifiedAt}
                        title={new Date(resource.verifiedAt).toLocaleString()}
                      >
                        Verified {relativeTime(resource.verifiedAt)}
                      </time>
                    ) : (
                      <time
                        dateTime={resource.createdAt}
                        title={new Date(resource.createdAt).toLocaleString()}
                      >
                        Added {relativeTime(resource.createdAt)}
                      </time>
                    )}
                  </>
                }
              >
                <Cluster className="app-resource-actions">
                  <Button
                    size="sm"
                    aria-label={`Edit ${labels[type]} variables`}
                    onClick={() => setEditing(resource.resourceId)}
                  >
                    Edit variables
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={`Verify ${labels[type]}`}
                    disabled={disabled}
                    onClick={() => request({ resource: resource.resourceId, action: 'verify' })}
                  >
                    Verify
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={`Rotate ${labels[type]} credentials`}
                    disabled={disabled}
                    onClick={() => request({ resource: resource.resourceId, action: 'rotate' })}
                  >
                    Rotate credentials
                  </Button>
                </Cluster>
              </ListItem>
            );
          })}
        </List>
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
        Databases are backed up every night. {configurationGuidance.postgres} Only an admin can
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
          onApply={(outputs) => {
            replace(editingResource.resourceId, outputs);
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
              disabled={identityProvider && !identityConfirmed}
              onClick={() => {
                if (confirming)
                  action.mutate({ ...confirming, identity: identityProvider && identityConfirmed });
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
        {confirming?.action === 'rotate' && (
          <p>
            {confirmingLabel} gets new credentials. Your app picks them up on its next deploy, so
            deploy again after rotating.
          </p>
        )}
        {identityProvider && (
          <Checkbox
            label="Signing in to this portal depends on this app. Continue anyway."
            checked={identityConfirmed}
            onChange={(event) => setIdentityConfirmed(event.target.checked)}
          />
        )}
      </Dialog>
    </Section>
  );
}
