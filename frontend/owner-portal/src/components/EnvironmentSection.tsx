import {
  Alert,
  Button,
  Cluster,
  Dialog,
  ErrorAlert,
  Field,
  Hint,
  InlineStatus,
  Input,
  List,
  ListItem,
  LoadingRows,
  RelativeTime,
  Section,
} from '@openstack-platform/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { api, resourceApi, validateEnvName, type StorageBinding } from '../api';
import { useIntentPolling } from '../hooks/useIntentPolling';
import { QueryError } from './Feedback';
import { Operation, OperationList } from './Operation';
import '../pages/app-pages.css';

export function EnvironmentSection({
  id,
  bindings,
  service = api,
}: {
  id: string;
  bindings: StorageBinding[];
  service?: ReturnType<typeof resourceApi>;
}) {
  const environment = useQuery({
    queryKey: ['environment', id],
    queryFn: () => service.environment(id),
    refetchInterval: 5000,
  });
  const [name, setName] = useState('');
  const [removing, setRemoving] = useState<string | null>(null);
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
        ? service.setEnvironment(id, target, value, key)
        : service.deleteEnvironment(id, target, key);
    },
    onSuccess: (result) => {
      setIntentId(result.intentId);
      client.invalidateQueries({ queryKey: ['intents'] });
      client.invalidateQueries({ queryKey: ['environment', id] });
    },
  });
  const recoverable =
    environment.data?.intents?.filter((item) => item.requiresResubmit && item.retryKey) ?? [];
  const items = environment.data?.items ?? [];
  return (
    <Section title="Environment variables" flush>
      {environment.isPending ? (
        <LoadingRows rows={2} />
      ) : environment.error ? (
        <div className="app-block">
          <QueryError query={environment} what="your environment variables" />
        </div>
      ) : items.length ? (
        <List label="Environment variables">
          {items.map((item) => (
            <ListItem
              key={item.name}
              title={<code>{item.name}</code>}
              meta={
                item.updatedAt && (
                  <span>
                    Updated <RelativeTime value={item.updatedAt} />
                  </span>
                )
              }
              trailing={
                <Cluster gap={1}>
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={`Replace ${item.name}`}
                    onClick={() => {
                      setName(item.name);
                      setError(null);
                      valueField.current?.focus();
                    }}
                  >
                    Replace
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={`Delete ${item.name}`}
                    disabled={edit.isPending || !!busy}
                    onClick={() => setRemoving(item.name)}
                  >
                    Delete
                  </Button>
                </Cluster>
              }
            />
          ))}
        </List>
      ) : (
        <p className="app-block ui-text-muted">
          No variables yet. Add API keys and other settings your app reads at runtime.
        </p>
      )}
      {recoverable.map((item) => {
        const target = item.names?.[0];
        const deleting = item.kind === 'env_delete';
        return (
          <div className="app-block" key={item.intentId}>
            <Alert
              tone="warning"
              action={
                <Button
                  size="sm"
                  aria-label={`Finish change to ${target}`}
                  onClick={() => {
                    if (!target || !item.retryKey) return;
                    attempt.current = {
                      key: item.retryKey,
                      name: target,
                      action: deleting ? 'delete' : 'set',
                    };
                    setName(target);
                    setIntentId(item.intentId);
                    if (deleting) setRemoving(target);
                    else valueField.current?.focus();
                  }}
                >
                  Finish change
                </Button>
              }
            >
              {deleting
                ? `Deleting ${target} didn’t finish.`
                : `Your change to ${target} didn’t finish.`}
            </Alert>
          </div>
        );
      })}
      <form
        className="app-block app-block--subtle"
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          const invalid =
            validateEnvName(name) ??
            (bindings.some((binding) => Object.values(binding.outputs).includes(name))
              ? `${name} is already set by a database or storage connection. Choose another name.`
              : null);
          setError(invalid);
          if (!invalid) edit.mutate({ action: 'set', name });
        }}
      >
        <div className="app-env-form">
          <Field label="Variable name" id="env-name">
            <Input
              className="app-mono-input"
              value={name}
              autoComplete="off"
              autoCapitalize="characters"
              spellCheck={false}
              placeholder="API_KEY"
              onChange={(event) => setName(event.target.value)}
              required
            />
          </Field>
          <Field label="New value" id="env-value">
            <Input type="password" ref={valueField} autoComplete="new-password" />
          </Field>
          <Button type="submit" disabled={edit.isPending || !!busy} loading={edit.isPending}>
            Save variable
          </Button>
        </div>
        <Hint>
          Values are never shown again after you save. Saving restarts your app if it’s running.
        </Hint>
        <ErrorAlert error={error ?? edit.error} />
        {intent.data?.requiresResubmit && (
          <InlineStatus tone="warning">
            Enter the same value again and save to finish this change. Values are never stored in
            your browser.
          </InlineStatus>
        )}
      </form>
      {intent.data && (
        <div className="app-divider">
          <OperationList label="Variable changes">
            <Operation intent={intent.data} showApp={false} />
          </OperationList>
        </div>
      )}
      <Dialog
        open={removing !== null}
        onClose={() => setRemoving(null)}
        size="sm"
        title={`Delete ${removing ?? ''}?`}
        footer={
          <>
            <Button onClick={() => setRemoving(null)}>Cancel</Button>
            <Button
              variant="danger"
              onClick={() => {
                if (removing) edit.mutate({ action: 'delete', name: removing });
                setRemoving(null);
              }}
            >
              Delete variable
            </Button>
          </>
        }
      >
        <p>Your app stops receiving this variable. If it’s running, it restarts.</p>
      </Dialog>
    </Section>
  );
}
