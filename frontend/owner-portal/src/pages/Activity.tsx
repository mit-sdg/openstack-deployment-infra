import {
  Button,
  Cluster,
  EmptyState,
  Page,
  PageHeader,
  Section,
  buttonClass,
} from '@openstack-platform/ui';
import { useState } from 'react';
import { Link, useSearch } from 'wouter';
import { classApi } from '../classApi';
import { OperationList, needsAttention } from '../components/Operation';
import { ActivityEmpty, ActivityItem, Loaded, pager } from '../components/ClassRecords';
import { useRead, LIST } from '../hooks/useClassReads';
export function ActivityPage() {
  const search = useSearch();
  return <Activity key={search} search={search} />;
}
function Activity({ search }: { search: string }) {
  const params = new URLSearchParams(search);
  const ownerId = params.get('ownerId') ?? undefined;
  const applicationId = params.get('applicationId') ?? undefined;
  const [cursor, setCursor] = useState<string>();
  const [attentionCursor, setAttentionCursor] = useState<string>();
  const attention = useRead(
    ['attention', ownerId, applicationId, attentionCursor],
    (signal) => classApi.activity(ownerId, applicationId, attentionCursor, signal, true),
    { poll: LIST },
  );
  const activity = useRead(
    ['activity', ownerId, applicationId, cursor],
    (signal) => classApi.activity(ownerId, applicationId, cursor, signal),
    { enabled: !attention.isPending, poll: LIST },
  );
  return (
    <Page>
      <PageHeader title="Activity" />
      {(ownerId || applicationId) && (
        <Cluster>
          <span className="ui-text-muted">Filtered by {ownerId ? 'person' : 'app'}</span>
          <Link className={buttonClass({ variant: 'ghost', size: 'sm' })} href="/activity">
            Clear filter
          </Link>
        </Cluster>
      )}
      <Section
        title="Needs attention"
        flush
        footer={pager(attention.data, attentionCursor, setAttentionCursor)}
      >
        <Loaded query={attention} what="activity that needs attention">
          {(data) =>
            data.items.length ? (
              <OperationList label="Activity that needs attention">
                {data.items.map((item) => (
                  <ActivityItem key={item.intentId} item={item} />
                ))}
              </OperationList>
            ) : (
              <EmptyState title="Nothing needs attention">
                Blocked changes will appear here with a Resume action.
              </EmptyState>
            )
          }
        </Loaded>
      </Section>
      <Section title="Recent activity" flush footer={pager(activity.data, cursor, setCursor)}>
        <Loaded query={activity} what="activity" rows={5}>
          {(data) =>
            data.items.length ? (
              <OperationList label="Recent activity">
                {data.items
                  .filter((item) => !needsAttention(item))
                  .map((item) => (
                    <ActivityItem key={item.intentId} item={item} />
                  ))}
              </OperationList>
            ) : (
              <ActivityEmpty filtered={!!(ownerId || applicationId)} />
            )
          }
        </Loaded>
      </Section>
    </Page>
  );
}
