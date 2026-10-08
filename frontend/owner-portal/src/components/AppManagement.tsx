import {
  Alert,
  Button,
  Dialog,
  ErrorAlert,
  Field,
  Input,
  List,
  ListItem,
  Section,
  Select,
} from '@openstack-platform/ui';
import { useState, type FormEvent, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type AppRecord, type StorageResource } from '../api';
import { appManagementApi } from '../appManagementApi';
import { OwnerPicker, friendly, shown } from '../pages/admin/common';
const storageNames = { postgres: 'PostgreSQL', mongo: 'MongoDB', s3: 'S3 storage' };
function SignInInfo() {
  return <Alert tone="info">Signing in to this portal depends on this app.</Alert>;
}
export function AppManagement({ id }: { id: string }) {
  const client = useQueryClient();
  const app = useQuery({ queryKey: ['app', id], queryFn: () => api.app(id) });
  const storage = useQuery({
    queryKey: ['storage', id],
    queryFn: () => api.storage(id),
    enabled: app.data?.access === 'admin',
  });
  const [action, setAction] = useState<'owner' | 'storage' | null>(null);
  if (app.data?.access !== 'admin' || !app.data.ownerId) return null;
  const data = app.data as AppRecord & { ownerId: string };
  const resources = storage.data?.items ?? [];
  function refresh() {
    setAction(null);
    void client.invalidateQueries({ queryKey: ['app', id] });
    void client.invalidateQueries({ queryKey: ['storage', id] });
    void client.invalidateQueries({ queryKey: ['all-apps'] });
  }
  return (
    <>
      <Section title="Danger zone" aria-label="Danger zone" flush>
        <List label="Danger zone">
          <ListItem
            title="Change owner"
            meta={data.ownerDisplayName ?? 'Move this app to another account.'}
            trailing={
              <Button size="sm" onClick={() => setAction('owner')}>
                Change owner
              </Button>
            }
          />
          <ListItem
            title="Delete a database or storage"
            meta={
              resources.length
                ? 'Permanently delete it and all of its data.'
                : 'This app has no databases or storage.'
            }
            trailing={
              <Button
                size="sm"
                variant="danger"
                disabled={!resources.length}
                onClick={() => setAction('storage')}
              >
                Delete
              </Button>
            }
          />
        </List>
      </Section>
      <OwnerDialog
        open={action === 'owner'}
        onClose={() => setAction(null)}
        app={data}
        onDone={refresh}
      />
      <StorageDialog
        open={action === 'storage'}
        onClose={() => setAction(null)}
        app={data}
        resources={resources}
        onStarted={refresh}
      />
    </>
  );
}
function ActionDialog({
  open,
  onClose,
  title,
  form,
  submit,
  pending,
  disabled = false,
  danger = false,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  form: string;
  submit: string;
  pending: boolean;
  disabled?: boolean;
  danger?: boolean;
  children: ReactNode;
}) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={title}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            type="submit"
            form={form}
            variant={danger ? 'danger' : 'primary'}
            loading={pending}
            disabled={disabled}
          >
            {submit}
          </Button>
        </>
      }
    >
      {children}
    </Dialog>
  );
}

function OwnerDialog({
  open,
  onClose,
  app,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  app: AppRecord & { ownerId: string };
  onDone: () => void;
}) {
  const identity = app.identityProvider;
  const [owner, setOwner] = useState('');
  const reassign = useMutation({
    mutationFn: () => appManagementApi.reassign(app.applicationId, app.ownerId, owner),
    onSuccess: onDone,
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    reassign.mutate();
  }
  return (
    <ActionDialog
      open={open}
      onClose={onClose}
      title="Change owner"
      form="admin-owner"
      submit="Change owner"
      pending={reassign.isPending}
      disabled={!owner || owner === app.ownerId}
    >
      <form id="admin-owner" className="ui-stack ui-gap-4" onSubmit={submit}>
        <ErrorAlert error={friendly(reassign.error)} />
        <OwnerPicker value={owner} onChange={setOwner} />
        {identity && <SignInInfo />}
      </form>
    </ActionDialog>
  );
}

function StorageDialog({
  open,
  onClose,
  app,
  resources,
  onStarted,
}: {
  open: boolean;
  onClose: () => void;
  app: AppRecord & { ownerId: string };
  resources: StorageResource[];
  onStarted: (result: { intentId: string }) => void;
}) {
  const identity = app.identityProvider;
  const [resource, setResource] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const chosen = resources.find((r) => r.resourceId === resource);
  const phrase = chosen ? `${app.slug} ${chosen.type}` : '';
  const remove = useMutation({
    mutationFn: () =>
      appManagementApi.deleteStorage(
        app.applicationId,
        resource,
        confirmation,
        crypto.randomUUID(),
      ),
    onSuccess: (result) => {
      setResource('');
      setConfirmation('');
      onStarted(result);
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    remove.mutate();
  }
  return (
    <ActionDialog
      open={open}
      onClose={onClose}
      title="Delete a database or storage"
      form="admin-storage"
      submit="Delete permanently"
      danger
      pending={remove.isPending}
      disabled={!chosen || confirmation !== phrase}
    >
      <form id="admin-storage" className="ui-stack ui-gap-4" onSubmit={submit}>
        <ErrorAlert error={shown(remove.error)} />
        <Alert tone="danger" title="This can’t be undone">
          All of its data is deleted. Restoring a nightly backup needs the platform team and can
          lose recent changes. First remove it from the app’s settings and save.
        </Alert>
        <Field label="Database or storage" id="delete-resource">
          <Select
            required
            value={resource}
            onChange={(event) => {
              setResource(event.target.value);
              setConfirmation('');
            }}
          >
            <option value="">Choose one</option>
            {resources.map((r) => (
              <option key={r.resourceId} value={r.resourceId}>
                {storageNames[r.type] ?? r.type} · {r.label}
              </option>
            ))}
          </Select>
        </Field>
        {chosen && (
          <Field label={`Type “${phrase}” to confirm`} id="storage-confirm">
            <Input
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              value={confirmation}
              onChange={(event) => setConfirmation(event.target.value)}
            />
          </Field>
        )}
        {identity && <SignInInfo />}
      </form>
    </ActionDialog>
  );
}
