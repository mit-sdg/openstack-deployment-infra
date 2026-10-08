import {
  Alert,
  Button,
  CopyField,
  Dialog,
  ErrorAlert,
  Hint,
  InlineStatus,
  LoadingRows,
  Section,
} from '@openstack-platform/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { api, type SourceAccess } from '../api';
import { short } from '../utils/presentation';

const problems: Record<string, string> = {
  'key-refused':
    'GitHub refused the key. Add it to the repository’s deploy keys, then check again.',
  'not-found': 'GitHub can’t find this repository with the key. Check the repository URL above.',
  unavailable: 'Couldn’t reach GitHub. Try again in a minute.',
};

function AccessResult({ access }: { access: SourceAccess }) {
  if (!access.keyPresent) return null;
  if (access.reachable && access.head)
    return (
      <InlineStatus>
        GitHub accepts the key. {access.branch} is at {short(access.head)}.
      </InlineStatus>
    );
  return (
    <Alert tone="warning">
      {access.problem === 'branch-missing'
        ? `The repository has no branch named ${access.branch}.`
        : problems[access.problem ?? 'unavailable']}
    </Alert>
  );
}

/**
 * Deploy key for a private repository. The platform keeps the private half;
 * the owner adds the public half to the repository on GitHub, read-only.
 */
export function RepositoryAccess({
  id,
  service = api,
  saved,
}: {
  id: string;
  service?: Pick<
    typeof api,
    'sourceKey' | 'createSourceKey' | 'removeSourceKey' | 'checkSourceKey'
  >;
  /** Whether a repository is saved, so access can be checked. */
  saved: boolean;
}) {
  const client = useQueryClient();
  const [confirming, setConfirming] = useState<'replace' | 'remove' | null>(null);
  const key = useQuery({
    queryKey: ['source-key', id],
    queryFn: () => service.sourceKey(id),
    retry: false,
  });
  const create = useMutation({
    mutationFn: (replace: boolean) => service.createSourceKey(id, replace),
    onSuccess: (data) => {
      client.setQueryData(['source-key', id], data);
      check.reset();
      setConfirming(null);
    },
  });
  const remove = useMutation({
    mutationFn: () => service.removeSourceKey(id),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ['source-key', id] });
      check.reset();
      create.reset();
      setConfirming(null);
    },
  });
  const check = useMutation({ mutationFn: () => service.checkSourceKey(id) });
  return (
    <Section
      title="Private repository"
      aria-label="Private repository"
      actions={
        key.data?.present && (
          <>
            <Button size="sm" variant="ghost" onClick={() => setConfirming('replace')}>
              Replace key
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setConfirming('remove')}>
              Remove key
            </Button>
          </>
        )
      }
    >
      {key.isPending ? (
        <LoadingRows rows={2} />
      ) : key.error ? (
        <ErrorAlert error={key.error} />
      ) : !key.data.present ? (
        <>
          <Hint>
            Public repositories need nothing here. For a private one, create a deploy key and add it
            to the repository on GitHub. It lets the platform read that repository, and nothing
            else.
          </Hint>
          <ErrorAlert error={create.error} />
          <div>
            <Button loading={create.isPending} onClick={() => create.mutate(false)}>
              Create deploy key
            </Button>
          </div>
        </>
      ) : (
        <>
          <CopyField
            label="Deploy key"
            value={key.data.publicKey}
            hint={
              <>
                On GitHub, open the repository’s Settings → Deploy keys → Add deploy key, paste this
                key and leave “Allow write access” off. Fingerprint{' '}
                <code className="ui-break">{key.data.fingerprint}</code>.
              </>
            }
          />
          <ErrorAlert error={check.error ?? create.error} />
          {check.data && <AccessResult access={check.data} />}
          <div>
            <Button loading={check.isPending} disabled={!saved} onClick={() => check.mutate()}>
              Check access
            </Button>
          </div>
          {!saved && <Hint>Save a repository in the settings above to check access.</Hint>}
        </>
      )}
      <Dialog
        open={confirming === 'replace'}
        onClose={() => setConfirming(null)}
        title="Replace the deploy key?"
        size="sm"
        footer={
          <>
            <Button onClick={() => setConfirming(null)}>Cancel</Button>
            <Button variant="danger" loading={create.isPending} onClick={() => create.mutate(true)}>
              Replace key
            </Button>
          </>
        }
      >
        <p>
          The current key stops working right away. Add the new key to the repository on GitHub and
          remove the old one there.
        </p>
        <ErrorAlert error={create.error} />
      </Dialog>
      <Dialog
        open={confirming === 'remove'}
        onClose={() => setConfirming(null)}
        title="Remove this deploy key?"
        size="sm"
        footer={
          <>
            <Button onClick={() => setConfirming(null)}>Cancel</Button>
            <Button variant="danger" loading={remove.isPending} onClick={() => remove.mutate()}>
              Remove key
            </Button>
          </>
        }
      >
        <p>
          Builds of a private repository will fail until you add a new key. Also delete the key from
          the repository’s Deploy keys on GitHub.
        </p>
        <ErrorAlert error={remove.error} />
      </Dialog>
    </Section>
  );
}
