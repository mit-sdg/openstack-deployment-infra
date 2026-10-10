import {
  Alert,
  Button,
  Dialog,
  ErrorAlert,
  Field,
  Input,
  List,
  ListItem,
} from '@openstack-platform/ui';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useLocation } from 'wouter';
import { type AppRecord, type Session } from '../api';
import { appManagementApi } from '../appManagementApi';
import { useIntentPolling } from '../hooks/useIntentPolling';
import { shown, useStepUp } from '../pages/admin/common';
import { Operation, OperationList } from './Operation';

export function DeleteApp({ app }: { app: AppRecord }) {
  const client = useQueryClient();
  const [, navigate] = useLocation();
  const session = useQuery<Session>({ queryKey: ['session'], enabled: false });
  const step = useStepUp();
  const [open, setOpen] = useState(false);
  const [confirmation, setConfirmation] = useState('');
  const key = useRef(crypto.randomUUID());
  const [intentId, setIntentId] = useState<string | null>(null);
  const intent = useIntentPolling(intentId);
  const deleted = intent.data?.state === 'succeeded';
  const remove = useMutation({
    mutationFn: () =>
      step.run(() => appManagementApi.deleteApp(app.applicationId, confirmation, key.current)),
    onSuccess: (result) => {
      client.setQueryData(['intent', result.intentId], result);
      setIntentId(result.intentId);
    },
  });
  useEffect(() => {
    if (!deleted) return;
    for (const queryKey of [['all-apps'], ['apps'], ['class'], ['intents'], ['session']]) {
      void client.invalidateQueries({ queryKey });
    }
  }, [client, deleted]);
  if (session.data?.role !== 'admin') return null;
  function close() {
    if (remove.isPending) return;
    setOpen(false);
    if (deleted) {
      navigate('/all-apps');
      const appQueries = {
        predicate: (query: { queryKey: readonly unknown[] }) =>
          query.queryKey[0] !== 'intent' && query.queryKey.includes(app.applicationId),
      };
      void client.cancelQueries(appQueries).then(() => client.removeQueries(appQueries));
    }
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    if (confirmation === app.slug && !intentId && !remove.isPending) remove.mutate();
  }
  const progress = intent.data ? (
    <OperationList label="App deletion">
      <Operation intent={intent.data} showApp={false} />
    </OperationList>
  ) : null;
  return (
    <>
      <List label="Delete app">
        <ListItem
          title="Permanently delete this app"
          meta="Remove the app and all of its data."
          trailing={
            <Button variant="danger" size="sm" onClick={() => setOpen(true)}>
              Delete app
            </Button>
          }
        />
      </List>
      {!open && progress}
      <Dialog
        open={open && !step.prompting}
        onClose={close}
        title="Delete app"
        footer={
          <>
            <Button onClick={close} disabled={remove.isPending}>
              {deleted ? 'Done' : 'Cancel'}
            </Button>
            {!intentId && (
              <Button
                type="submit"
                form="delete-app"
                variant="danger"
                loading={remove.isPending}
                disabled={confirmation !== app.slug}
              >
                Delete permanently
              </Button>
            )}
          </>
        }
      >
        {intentId ? (
          <div className="ui-stack ui-gap-4">
            {deleted && <Alert tone="success">The app and its data have been deleted.</Alert>}
            <ErrorAlert error={intent.error} />
            {progress}
          </div>
        ) : (
          <form id="delete-app" className="ui-stack ui-gap-4" onSubmit={submit}>
            <ErrorAlert error={shown(remove.error)} />
            <Alert tone="danger" title="This can’t be undone">
              This permanently removes the running app and its worker, every database and all its
              data, the S3 bucket and all its objects, environment variables, the deploy key, and
              all members. Recovery is possible only from platform backups and needs the platform
              team. Changes since the backup will be lost.
            </Alert>
            <p>
              Last successful platform backup: unavailable in 6.1040 Apps. Check with the platform
              team before deleting.
            </p>
            {app.identityProvider && (
              <Alert tone="warning">
                Signing in to 6.1040 Apps depends on this app. Deleting it will interrupt class
                sign-in.
              </Alert>
            )}
            <Field label={`Type “${app.slug}” to confirm`} id="delete-app-confirm">
              <Input
                required
                autoComplete="off"
                autoCapitalize="none"
                spellCheck={false}
                value={confirmation}
                onChange={(event) => setConfirmation(event.target.value)}
              />
            </Field>
          </form>
        )}
      </Dialog>
      {step.dialog}
    </>
  );
}
