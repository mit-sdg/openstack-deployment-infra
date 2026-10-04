/** "owner/repo" for GitHub URLs, otherwise host and path. */
export function repositoryName(url: string) {
  try {
    const { hostname, pathname } = new URL(url);
    const path = pathname.replace(/^\/|\.git$|\/$/g, '');
    return hostname === 'github.com' && path ? path : hostname + (path ? `/${path}` : '');
  } catch {
    return url;
  }
}

/** A repository link labelled with its short name. */
export function Repository({ url }: { url: string | null }) {
  if (!url) return <span className="ui-text-subtle">Not set</span>;
  return (
    <a className="ui-link ui-break" href={url} target="_blank" rel="noopener noreferrer">
      {repositoryName(url)}
    </a>
  );
}
