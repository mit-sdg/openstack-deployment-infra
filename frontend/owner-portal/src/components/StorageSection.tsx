import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import {
  api,
  configurationGuidance,
  resourceApi,
  validateBindings,
  type StorageBinding,
  type StorageResource,
} from '../api';
import { time } from '../utils/presentation';
import { ErrorNotice, Loading } from './Feedback';
import { Operation } from './Operation';

const labels = { postgres: 'PostgreSQL', mongo: 'MongoDB', s3: 'S3 bucket' };
export function StorageSection({
  id,
  bindings,
  onChange,
  service = api,
}: {
  id: string;
  service?: ReturnType<typeof resourceApi>;
  bindings: StorageBinding[];
  onChange: (bindings: StorageBinding[]) => void;
}) {
  const scope = service === api ? [] : ['admin'];
  const client = useQueryClient();
  const storage = useQuery({
    queryKey: [...scope, 'storage', id],
    queryFn: () => service.storage(id),
    refetchInterval: service === api ? 1500 : 5000,
    refetchOnWindowFocus: service === api,
  });
  const environment = useQuery({
    queryKey: [...scope, 'environment', id],
    queryFn: () => service.environment(id),
  });
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
  const [error, setError] = useState<string | null>(null);
  const pending = useRef<{ subject: string; key: string } | null>(null);
  const action = useMutation({
    mutationFn: ({
      type,
      resource,
      action,
    }: {
      type?: StorageResource['type'];
      resource?: string;
      action?: 'rotate' | 'verify';
    }) => {
      const subject = type ?? `${resource}/${action}`;
      const key = pending.current?.subject === subject ? pending.current.key : crypto.randomUUID();
      pending.current = { subject, key };
      return type
        ? service.createStorage(id, type, key)
        : service.storageAction(id, resource!, action!, key);
    },
    onSuccess: (_result, variables) => {
      if (variables.type) setCreatedType(variables.type);
      pending.current = null;
      client.invalidateQueries({ queryKey: [...scope, 'storage', id] });
      client.invalidateQueries({ queryKey: ['intents'] });
    },
  });
  const invalid = validateBindings(
    bindings,
    environment.data?.items.map((item) => item.name) ?? [],
  );
  function output(resource: string, name: string, target: string | null) {
    const current = { ...(bindings.find((item) => item.resourceId === resource)?.outputs ?? {}) };
    if (target === null) delete current[name];
    else current[name] = target;
    onChange([
      ...bindings.filter((item) => item.resourceId !== resource),
      ...(Object.keys(current).length ? [{ resourceId: resource, outputs: current }] : []),
    ]);
  }
  return (
    <section className="section card form-card">
      <div className="form-section">
        <h2>Databases and storage</h2>
        <p>One of each type per application. Databases are backed up nightly by the platform.</p>
        <p className="field-help">
          {configurationGuidance.postgres} Individual password and S3 secret key bindings need a
          platform update. PostgreSQL and S3 use TLS with the platform CA, delivered through
          PGSSLROOTCERT and AWS_CA_BUNDLE by default. Keep those outputs bound when using TLS
          verification.
        </p>
        <p>Binding changes apply on your next deploy. Save configuration after editing bindings.</p>
        <div className="button-group">
          {(['postgres', 'mongo', 's3'] as const).map((type) => (
            <button
              className="button"
              key={type}
              type="button"
              disabled={
                storage.isPending ||
                !!storage.error ||
                action.isPending ||
                busy ||
                storage.data?.items.some((item) => item.type === type) ||
                storage.data?.intents.some(
                  (intent) =>
                    intent.kind === 'storage_create' &&
                    intent.type === type &&
                    !['succeeded', 'failed'].includes(intent.state),
                )
              }
              onClick={() => action.mutate({ type })}
            >
              Add {labels[type]}
            </button>
          ))}
        </div>
        <ErrorNotice error={storage.error ?? action.error ?? error} />
        {storage.isPending && <Loading />}
        {storage.data?.items.map((resource) => {
          const current =
            bindings.find((item) => item.resourceId === resource.resourceId)?.outputs ?? {};
          return (
            <div className="form-section" key={resource.resourceId}>
              <h3>
                {resource.label} <span className="chip">{resource.status}</span>
              </h3>
              <p className="field-help">
                Created {time(resource.createdAt)} · Verified {time(resource.verifiedAt)}
              </p>
              <fieldset>
                <legend>{labels[resource.type]} bindings</legend>
                {Object.entries(resource.defaultBindings).map(([name, defaultTarget]) => (
                  <div className="field" key={name}>
                    <label htmlFor={`${resource.resourceId}-${name}`}>
                      {name} → environment name
                    </label>
                    {name in current ? (
                      <div className="button-group">
                        <input
                          id={`${resource.resourceId}-${name}`}
                          value={current[name]}
                          onChange={(event) =>
                            output(resource.resourceId, name, event.target.value)
                          }
                        />
                        <button
                          type="button"
                          className="button button-small"
                          onClick={() => output(resource.resourceId, name, null)}
                        >
                          Remove {name} binding
                        </button>
                      </div>
                    ) : (
                      <button
                        type="button"
                        className="button button-small"
                        onClick={() => output(resource.resourceId, name, defaultTarget)}
                      >
                        Bind {name} to {defaultTarget}
                      </button>
                    )}
                  </div>
                ))}
                {Object.entries(resource.unavailableBindings ?? {}).map(([name, message]) => (
                  <div className="field" key={name}>
                    <p className="field-help">{message}</p>
                    {name in current && (
                      <button
                        type="button"
                        className="button button-small"
                        onClick={() => output(resource.resourceId, name, null)}
                      >
                        Remove unavailable {name} binding
                      </button>
                    )}
                  </div>
                ))}
                {!Object.keys(current).length && (
                  <button
                    type="button"
                    className="button"
                    onClick={() =>
                      onChange([
                        ...bindings.filter((item) => item.resourceId !== resource.resourceId),
                        {
                          resourceId: resource.resourceId,
                          outputs: { ...resource.defaultBindings },
                        },
                      ])
                    }
                  >
                    Use default bindings
                  </button>
                )}
              </fieldset>
              <div className="button-group">
                <button
                  type="button"
                  className="button"
                  disabled={action.isPending || !!busy || resource.status !== 'ready'}
                  onClick={() => action.mutate({ resource: resource.resourceId, action: 'verify' })}
                >
                  Verify {labels[resource.type]}
                </button>
                <button
                  type="button"
                  className="button"
                  disabled={action.isPending || !!busy || resource.status !== 'ready'}
                  onClick={() => {
                    setError(null);
                    if (
                      window.confirm(
                        'Rotate credentials? Redeploy your app to pick up the new credentials.',
                      )
                    )
                      action.mutate({ resource: resource.resourceId, action: 'rotate' });
                  }}
                >
                  Rotate {labels[resource.type]} credentials
                </button>
              </div>
              <p>Rotation requires a redeploy to pick up the new credentials.</p>
              <p>
                Ask an administrator to delete this database
                {resource.type === 's3' ? ' or bucket' : ''}.
              </p>
            </div>
          );
        })}
        {invalid && <p role="alert">{invalid}</p>}
        <ul className="operation-list">
          {storage.data?.intents.slice(0, 6).map((intent) => (
            <Operation key={intent.intentId} intent={intent} />
          ))}
        </ul>
      </div>
    </section>
  );
}
