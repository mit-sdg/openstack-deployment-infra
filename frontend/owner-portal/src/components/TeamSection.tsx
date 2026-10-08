import {
  Button,
  DataTable,
  Dialog,
  ErrorAlert,
  Field,
  Hint,
  Input,
  LoadingRows,
  RelativeTime,
  Section,
  type Column,
} from '@openstack-platform/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useLocation } from 'wouter';
import { api, type TeamMember } from '../api';
import { useProviderLabel } from '../pages/admin/common';
import { QueryError } from './Feedback';

type Team = Awaited<ReturnType<typeof api.members>>;

/**
 * The people who work on an app. Members can do everything the owner can
 * except change the team; the app counts against the owner's limit only.
 */
export function TeamSection({
  id,
  service = api,
}: {
  id: string;
  service?: Pick<typeof api, 'members' | 'addMember' | 'removeMember'>;
}) {
  const key = ['members', id];
  const client = useQueryClient();
  const [, navigate] = useLocation();
  const provider = useProviderLabel();
  const [username, setUsername] = useState('');
  const [leaving, setLeaving] = useState<TeamMember | null>(null);
  const team = useQuery({ queryKey: key, queryFn: () => service.members(id) });
  const update = (data: Team) =>
    client.setQueryData(key, (current: Team | undefined) => ({ ...current, ...data }));
  const add = useMutation({
    mutationFn: () => service.addMember(id, username.trim()),
    onSuccess: (data) => {
      update(data);
      setUsername('');
    },
  });
  const remove = useMutation({
    mutationFn: (member: TeamMember) => service.removeMember(id, member.userId),
    onSuccess: (data) => {
      setLeaving(null);
      if (data.left) {
        client.invalidateQueries({ queryKey: ['apps'] });
        navigate('/apps');
        return;
      }
      update(data);
    },
  });
  const manages = team.data?.access !== 'member';
  const you = team.data?.you;
  const columns: Column<TeamMember>[] = [
    {
      key: 'name',
      header: 'Name',
      mobile: 'title',
      cell: (member) => (
        <span>
          {member.displayName}
          {member.userId === you && <span className="ui-text-muted"> (you)</span>}
        </span>
      ),
    },
    {
      key: 'role',
      header: 'Role',
      mobile: 'secondary',
      cell: (member) => (
        <span className="ui-text-muted">{member.role === 'owner' ? 'Owner' : 'Member'}</span>
      ),
    },
    {
      key: 'username',
      header: 'Username',
      mobile: 'hidden',
      cell: (member) => <span className="ui-text-muted">{member.username}</span>,
    },
    {
      key: 'added',
      header: 'Added',
      mobile: 'hidden',
      cell: (member) => (
        <span className="ui-text-muted">
          <RelativeTime value={member.addedAt} empty="—" />
        </span>
      ),
    },
    {
      key: 'actions',
      header: 'Actions',
      hideHeader: true,
      align: 'end',
      mobile: 'trailing',
      cell: (member) =>
        member.role === 'member' && (manages || member.userId === you) ? (
          <Button size="sm" variant="ghost" onClick={() => setLeaving(member)}>
            {member.userId === you ? 'Leave' : 'Remove'}
          </Button>
        ) : null,
    },
  ];
  function submit(event: FormEvent) {
    event.preventDefault();
    if (username.trim()) add.mutate();
  }
  const self = leaving?.userId === you;
  return (
    <Section
      title="Team"
      aria-label="Team"
      flush
      footer={
        manages && (
          <form className="app-team-add" onSubmit={submit}>
            <Field label="Add by username" id={`team-add-${id}`}>
              <Input
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                autoCapitalize="none"
                autoComplete="off"
                spellCheck={false}
                maxLength={64}
              />
            </Field>
            <Button type="submit" loading={add.isPending} disabled={!username.trim()}>
              Add to team
            </Button>
          </form>
        )
      }
    >
      {team.isPending ? (
        <LoadingRows rows={2} />
      ) : team.error ? (
        <QueryError query={team} what="the team" />
      ) : (
        <>
          <DataTable
            label="Team"
            columns={columns}
            rows={team.data.items}
            rowKey={(member) => member.userId}
          />
          <div className="app-team-notes">
            <ErrorAlert error={add.error ?? remove.error} />
            <Hint>
              {manages
                ? `Teammates can change settings, deploy and see logs. They need to sign in once with their ${provider.toLowerCase()} before you add them. The owner, staff and admins can add or remove people.`
                : 'You can change settings, deploy and see logs. The owner, staff and admins can add or remove people.'}
            </Hint>
          </div>
        </>
      )}
      <Dialog
        open={!!leaving}
        onClose={() => setLeaving(null)}
        title={self ? 'Leave this app?' : `Remove ${leaving?.displayName ?? ''}?`}
        size="sm"
        footer={
          <>
            <Button onClick={() => setLeaving(null)}>Cancel</Button>
            <Button
              variant="danger"
              loading={remove.isPending}
              onClick={() => leaving && remove.mutate(leaving)}
            >
              {self ? 'Leave app' : 'Remove'}
            </Button>
          </>
        }
      >
        <p>
          {self
            ? 'You’ll lose access to this app. The owner can add you again.'
            : 'They’ll lose access to this app. You can add them again later.'}
        </p>
      </Dialog>
    </Section>
  );
}
