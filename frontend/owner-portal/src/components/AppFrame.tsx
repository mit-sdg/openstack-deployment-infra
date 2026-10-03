import {
  ErrorAlert,
  Icon,
  Page,
  PageHeader,
  Skeleton,
  TabNav,
  backLinkClass,
  tabClass,
} from '@openstack-platform/ui';
import { useQuery } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { Link } from 'wouter';
import { api } from '../api';
import { healthy } from '../utils/presentation';
import { BoundaryText } from './BoundaryText';
import { Status } from './Status';
import '../pages/app-pages.css';

const tabs = [
  ['Overview', ''],
  ['Settings', '/configuration'],
  ['Deploy', '/deploy'],
  ['Deployments', '/deployments'],
] as const;

/** App header (name, status, URL) and the app's tabs. */
export function AppFrame({
  id,
  active,
  children,
}: {
  id: string;
  active: (typeof tabs)[number][0] | 'Configuration';
  children: ReactNode;
}) {
  const app = useQuery({
    queryKey: ['app', id],
    queryFn: () => api.app(id),
    refetchInterval: 5000,
  });
  const current = active === 'Configuration' ? 'Settings' : active;
  return (
    <Page>
      <PageHeader
        back={
          <Link href="/apps" className={backLinkClass}>
            <Icon name="arrow-left" />
            Apps
          </Link>
        }
        title={app.data ? app.data.slug : <Skeleton variant="title" width="quarter" />}
        meta={
          app.data && (
            <Status
              state={app.data.lifecycleState === 'creating' ? 'creating' : healthy(app.data)}
              label={
                app.data.lifecycleState !== 'creating' && !app.data.acceptedDeployment
                  ? 'Not deployed'
                  : undefined
              }
            />
          )
        }
      >
        {app.data?.url && (
          <a
            className="ui-link app-url"
            href={app.data.url}
            target="_blank"
            rel="noopener noreferrer"
          >
            <BoundaryText text={new URL(app.data.url).hostname} />
            <Icon name="external" />
          </a>
        )}
      </PageHeader>
      <TabNav label="App pages">
        {tabs.map(([name, suffix]) => (
          <Link
            key={name}
            href={`/apps/${id}${suffix}`}
            className={tabClass(current === name)}
            aria-current={current === name ? 'page' : undefined}
          >
            {name}
          </Link>
        ))}
      </TabNav>
      {app.error ? <ErrorAlert error={app.error} /> : children}
    </Page>
  );
}
