import {
  Alert,
  Button,
  Dialog,
  ErrorAlert,
  Hint,
  KeyValueList,
  RelativeTime,
  Spinner,
} from '@openstack-platform/ui';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import type { StorageResource } from '../api';
import { appManagementApi } from '../appManagementApi';
import { bytes } from './StorageUsage';
import '../pages/app-pages.css';

const names = { postgres: 'PostgreSQL', mongo: 'MongoDB', s3: 'S3 storage' };

/** Starts a download in the current tab; tests replace it. */
export const browser = { open: (url: string) => window.location.assign(url) };

/**
 * Staff take a snapshot of an app's database and download it. The file holds
 * people's data, so the dialog says so first, and each download fetches a
 * fresh link that works for a few minutes.
 */
export function SnapshotDialog({
  app,
  resource,
  onClose,
}: {
  app: string;
  resource: StorageResource;
  onClose: () => void;
}) {
  const name = names[resource.type];
  // One key per request, so a retried request reaches the same snapshot.
  const key = useRef(crypto.randomUUID());
  const [snapshot, setSnapshot] = useState<string | null>(null);
  const start = useMutation({
    mutationFn: () => appManagementApi.requestSnapshot(app, resource.resourceId, key.current),
    onSuccess: (result) => setSnapshot(result.snapshotId),
  });
  const status = useQuery({
    queryKey: ['snapshot', app, resource.resourceId, snapshot],
    queryFn: () => appManagementApi.snapshot(app, resource.resourceId, snapshot!),
    enabled: snapshot !== null,
    refetchInterval: (query) =>
      !query.state.data || query.state.data.state === 'preparing' ? 2000 : false,
  });
  const download = useMutation({
    mutationFn: () => appManagementApi.snapshotLink(app, resource.resourceId, snapshot!),
    onSuccess: (link) => browser.open(link.url),
  });
  function again() {
    key.current = crypto.randomUUID();
    setSnapshot(null);
    start.reset();
    download.reset();
  }
  const data = status.data;
  const state = snapshot === null ? 'confirm' : (data?.state ?? 'preparing');
  const error = start.error ?? download.error ?? (data ? null : status.error);
  const close = <Button onClick={onClose}>{state === 'confirm' ? 'Cancel' : 'Close'}</Button>;
  return (
    <Dialog
      open
      onClose={onClose}
      title={
        state === 'confirm'
          ? `Download a snapshot of ${name}?`
          : state === 'ready'
            ? 'Snapshot ready'
            : state === 'preparing'
              ? 'Preparing the snapshot'
              : 'No snapshot to download'
      }
      footer={
        state === 'confirm' ? (
          <>
            {close}
            <Button variant="primary" loading={start.isPending} onClick={() => start.mutate()}>
              Take snapshot
            </Button>
          </>
        ) : state === 'ready' ? (
          <>
            {close}
            <Button
              variant="primary"
              loading={download.isPending}
              onClick={() => download.mutate()}
            >
              Download
            </Button>
          </>
        ) : state === 'preparing' ? (
          close
        ) : (
          <>
            {close}
            <Button variant="primary" onClick={again}>
              Take a new snapshot
            </Button>
          </>
        )
      }
    >
      {state === 'confirm' && (
        <>
          <p>
            A snapshot is a copy of everything in this app’s {name} database, including personal
            data such as what people typed in while testing the app.
          </p>
          <p>
            Your request and every download are logged. Each download link works for 5 minutes, and
            the platform deletes the file a day after it’s taken. Keep your copy private and delete
            it when you’re done.
          </p>
        </>
      )}
      {state === 'preparing' && (
        <p className="app-inline">
          <Spinner />
          Copying the database. A large one can take a few minutes, so keep this open until it’s
          ready.
        </p>
      )}
      {state === 'ready' && data && (
        <>
          <KeyValueList
            items={[
              { label: 'File', value: <code>{data.filename}</code> },
              { label: 'Size', value: bytes(data.bytes ?? 0) },
              { label: 'Taken', value: <RelativeTime value={data.takenAt} /> },
              { label: 'Deleted', value: <RelativeTime value={data.expiresAt} /> },
              {
                label: 'SHA-256',
                value: <code className="ui-break">{data.sha256}</code>,
                stacked: true,
              },
            ]}
          />
          <Hint>
            {resource.type === 'mongo'
              ? 'The zip holds a JSON Lines file for each collection, a mongodump archive you can restore, and MANIFEST.json with checksums.'
              : 'The zip holds a SQL dump of the database and MANIFEST.json with checksums.'}{' '}
            Each download gets a new link.
          </Hint>
        </>
      )}
      {state === 'failed' && <Alert tone="danger">{data?.error}</Alert>}
      {state === 'expired' && (
        <Alert tone="info">This snapshot was deleted a day after it was taken.</Alert>
      )}
      <ErrorAlert error={error} />
    </Dialog>
  );
}
