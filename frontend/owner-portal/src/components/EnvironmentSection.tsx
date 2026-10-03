import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { api, validateEnvName, type StorageBinding } from '../api';
import { useIntentPolling } from '../hooks/useIntentPolling';
import { time } from '../utils/presentation';
import { ErrorNotice, Loading } from './Feedback';
import { Operation } from './Operation';

export function EnvironmentSection({ id, bindings }: { id: string; bindings: StorageBinding[] }) {
  const environment = useQuery({
    queryKey: ['environment', id],
    queryFn: () => api.environment(id),
    refetchInterval: 5000,
  });
  const [name, setName] = useState('');
  const valueField = useRef<HTMLInputElement>(null);
  const attempt = useRef<{ key: string; name: string; action: 'set' | 'delete' } | null>(null);
  const [intentId, setIntentId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const client = useQueryClient();
  const intent = useIntentPolling(intentId);
  const busy =
    intent.data && !['succeeded', 'failed', 'blocked', 'unknown'].includes(intent.data.state);
  useEffect(() => {
    if (intent.data?.state === 'succeeded' || intent.data?.state === 'failed') {
      attempt.current = null;
      client.invalidateQueries({ queryKey: ['environment', id] });
      client.invalidateQueries({ queryKey: ['app', id] });
    }
  }, [intent.data?.state, client, id]);
  const edit = useMutation({
    // Mutation variables contain only the action and name. Clear the write-only
    // DOM field before starting the request; values never enter query caches.
    mutationFn: ({ action, name: target }: { action: 'set' | 'delete'; name: string }) => {
      const previous = attempt.current;
      const key =
        previous?.action === action && previous.name === target
          ? previous.key
          : crypto.randomUUID();
      attempt.current = { key, name: target, action };
      const value = valueField.current?.value ?? '';
      if (valueField.current) valueField.current.value = '';
      return action === 'set'
        ? api.setEnvironment(id, target, value, key)
        : api.deleteEnvironment(id, target, key);
    },
    onSuccess: (result) => {
      setIntentId(result.intentId);
      client.invalidateQueries({ queryKey: ['intents'] });
      client.invalidateQueries({ queryKey: ['environment', id] });
    },
  });
  return (
    <section className="section card form-card">
      <div className="form-section">
        <h2>Environment variables</h2>
        <p>
          Values are write-only. Changes restart a running application and apply immediately after
          health checks pass.
        </p>
        {environment.isPending ? (
          <Loading />
        ) : environment.error ? (
          <ErrorNotice error={environment.error} />
        ) : (
          <div className="table-scroll">
            <table className="resource-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Environment updated</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {environment.data?.items.map((item) => (
                  <tr key={item.name}>
                    <td>
                      <code>{item.name}</code>
                    </td>
                    <td>{time(item.updatedAt)}</td>
                    <td>
                      <button
                        className="button button-small"
                        type="button"
                        onClick={() => {
                          setName(item.name);
                          valueField.current?.focus();
                        }}
                      >
                        Replace
                      </button>{' '}
                      <button
                        className="button button-small"
                        type="button"
                        disabled={edit.isPending || !!busy}
                        onClick={() => {
                          if (
                            window.confirm(
                              `Delete environment variable ${item.name}? This restarts a running application.`,
                            )
                          )
                            edit.mutate({ action: 'delete', name: item.name });
                        }}
                      >
                        Delete {item.name}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="field-help">
          The timestamp is for the whole environment revision. Existing values are never displayed.
        </p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            const invalid =
              validateEnvName(name) ??
              (bindings.some((binding) => Object.values(binding.outputs).includes(name))
                ? `${name} is a storage binding target.`
                : null);
            setError(invalid);
            if (!invalid) edit.mutate({ action: 'set', name });
          }}
        >
          <div className="fields-grid">
            <div className="field">
              <label htmlFor="env-name">Variable name</label>
              <input
                id="env-name"
                value={name}
                autoComplete="off"
                onChange={(event) => setName(event.target.value)}
                required
              />
            </div>
            <div className="field">
              <label htmlFor="env-value">New value</label>
              <input id="env-value" type="password" ref={valueField} autoComplete="new-password" />
            </div>
          </div>
          <ErrorNotice error={error ?? edit.error} />
          <button className="button button-primary" disabled={edit.isPending || !!busy}>
            Add or replace variable
          </button>
        </form>
        {environment.data?.intents
          ?.filter((item) => item.requiresResubmit && item.retryKey)
          .map((item) => (
            <button
              key={item.intentId}
              type="button"
              className="button"
              onClick={() => {
                const target = item.names?.[0];
                if (!target || !item.retryKey) return;
                attempt.current = {
                  key: item.retryKey,
                  name: target,
                  action: item.kind === 'env_delete' ? 'delete' : 'set',
                };
                setName(target);
                setIntentId(item.intentId);
                if (item.kind === 'env_delete') {
                  if (window.confirm(`Retry deleting ${target}?`))
                    edit.mutate({ action: 'delete', name: target });
                } else valueField.current?.focus();
              }}
            >
              Recover {item.kind === 'env_delete' ? 'deletion' : 'edit'} of {item.names?.[0]}
            </button>
          ))}
        {intent.data && (
          <ul className="operation-list">
            <Operation intent={intent.data} />
          </ul>
        )}
        {intent.data?.requiresResubmit && (
          <p role="status">
            Re-enter the same value and submit again to recover this edit. Its value was not saved
            by the portal.
          </p>
        )}
      </div>
    </section>
  );
}
