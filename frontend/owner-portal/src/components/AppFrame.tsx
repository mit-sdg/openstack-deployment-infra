import { Icon, Page, PageHeader, TabNav, backLinkClass, tabClass } from '@openstack-platform/ui';
import { useQuery } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { Link } from 'wouter';
import { api, type AppRecord } from '../api';
import { ownerAppState } from '../utils/presentation';
import { BoundaryText } from './BoundaryText';
import { QueryError } from './Feedback';
import { Status } from './Status';
import '../pages/app-pages.css';

const tabs = [
  ['Overview', ''],
  ['Settings', '/configuration'],
  ['Deploy', '/deploy'],
  ['Deployments', '/deployments'],
  ['Logs', '/logs'],
  ['Team', '/team'],
] as const;

/** The app's one state, shown next to its name (see ownerAppState). */
export function AppStatus({ app }: { app: AppRecord }) {
  return <Status state={ownerAppState(app)} />;
}

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
        title={
          app.data ? (
            app.data.slug
          ) : app.error ? (
            'App'
          ) : (
            <span className="ui-skeleton ui-skeleton--heading" aria-hidden="true" />
          )
        }
        meta={
          app.data ? (
            <AppStatus app={app.data} />
          ) : (
            app.isPending && <span className="ui-skeleton ui-skeleton--badge" aria-hidden="true" />
          )
        }
      >
        {app.isPending && (
          <span className="ui-skeleton-line" aria-hidden="true">
            <span className="ui-skeleton ui-skeleton--text ui-skeleton--quarter" />
          </span>
        )}
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
      {app.error ? (
        <QueryError query={app} what="this app" />
      ) : (
        <>
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
          {children}
        </>
      )}
    </Page>
  );
}
