import { LoadError } from '@openstack-platform/ui';
import { ApiError } from '../api';

/**
 * Says what failed and what to do. `what` is the plural thing the page shows,
 * e.g. "your apps": "Couldn't load your apps. Try again in a minute."
 */
export function loadErrorMessage(error: unknown, what: string) {
  if (error instanceof ApiError && error.status === 404)
    return `Couldn't find ${what}. Check the address, or go back and try again.`;
  if (error instanceof ApiError && error.status === 403)
    return `You don't have access to ${what}. Ask an admin if you need it.`;
  return `Couldn't load ${what}. Try again in a minute.`;
}

/**
 * Page- or section-level load failure for a query, with Retry. Render it
 * under the page's normal PageHeader (or inside the section) so the page
 * keeps its shape.
 */
export function QueryError({
  query,
  what,
}: {
  query: { error: unknown; refetch: () => unknown; isFetching: boolean };
  what: string;
}) {
  return (
    <LoadError onRetry={() => void query.refetch()} retrying={query.isFetching}>
      {loadErrorMessage(query.error, what)}
    </LoadError>
  );
}
