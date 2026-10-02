import { useQuery } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { Link } from 'wouter';
import { api } from '../api';
import { healthy } from '../utils/presentation';
import { BoundaryText } from './BoundaryText';
import { ErrorNotice } from './Feedback';
import { Status } from './Status';

export function AppFrame({
  id,
  active,
  children,
}: {
  id: string;
  active: string;
  children: ReactNode;
}) {
  const app = useQuery({
    queryKey: ['app', id],
    queryFn: () => api.app(id),
    refetchInterval: 5000,
  });
  return (
    <>
      <Link href="/apps" className="back-link">
        ← My applications
      </Link>
      {app.data && (
        <div className="app-heading">
          <div>
            <span className="eyebrow">Application</span>
            <h1>{app.data.slug}</h1>
            {app.data.url && (
              <a
                className="public-url mono"
                href={app.data.url}
                target="_blank"
                rel="noopener noreferrer"
              >
                <BoundaryText text={new URL(app.data.url).hostname} /> ↗
              </a>
            )}
          </div>
          <Status state={healthy(app.data)} />
        </div>
      )}
      <nav className="app-nav" aria-label="Application pages">
        {[
          ['Overview', ''],
          ['Configuration', '/configuration'],
          ['Deploy', '/deploy'],
          ['Deployments', '/deployments'],
        ].map(([name, suffix]) => (
          <Link
            key={name}
            href={`/apps/${id}${suffix}`}
            className={active === name ? 'active' : ''}
            aria-current={active === name ? 'page' : undefined}
          >
            {name}
          </Link>
        ))}
      </nav>
      {app.error ? <ErrorNotice error={app.error} /> : children}
    </>
  );
}
