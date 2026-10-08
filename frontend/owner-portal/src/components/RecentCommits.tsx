import { Fieldset, Hint, LoadingRows, Radio, RelativeTime } from '@openstack-platform/ui';
import { useQuery } from '@tanstack/react-query';
import type { SourceReadOptions } from '../api';
import { useId } from 'react';
import { GitHubError, recentCommits, type RecentCommit } from '../utils/github';
import '../pages/app-pages.css';

const problems = {
  'not-found':
    'GitHub didn’t list commits for this repository and branch. Paste a commit SHA instead.',
  'rate-limited':
    'GitHub’s hourly limit for listing commits was reached. Paste a commit SHA instead, or try again later.',
  unavailable: 'Couldn’t reach GitHub to list recent commits. Paste a commit SHA instead.',
};

/** Query for the newest commits on a branch, shared by pickers of the same branch. */
export function useRecentCommits(repository: string, branch: string, platform?: SourceReadOptions) {
  return useQuery({
    queryKey: ['github-commits', repository, branch, platform?.id ?? '', platform?.revision ?? 0],
    queryFn: async ({ signal }) => {
      try {
        return await recentCommits(repository, branch, signal);
      } catch (error) {
        if (signal.aborted || !platform || !(await platform.service.sourceKey(platform.id)).present)
          throw error;
        return platform.service.recentSourceCommits(platform.id);
      }
    },
    enabled: !!repository && !!branch,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}

/** Pick one of the newest commits on the branch; a SHA field stays the fallback. */
export function RecentCommits({
  repository,
  branch,
  value,
  onSelect,
  platform,
}: {
  repository: string;
  branch: string;
  value: string;
  onSelect: (commit: RecentCommit) => void;
  platform?: SourceReadOptions;
}) {
  const name = useId();
  const commits = useRecentCommits(repository, branch, platform);
  const legend = `Recent commits on ${branch}`;
  if (commits.isPending)
    return (
      <Fieldset legend={legend}>
        <LoadingRows rows={3} label="Loading recent commits…" />
      </Fieldset>
    );
  if (commits.error) {
    const problem = commits.error instanceof GitHubError ? commits.error.problem : 'unavailable';
    return <Hint>{problems[problem]}</Hint>;
  }
  if (!commits.data.length) return <Hint>{problems['not-found']}</Hint>;
  return (
    <Fieldset legend={legend} variant="cards" className="app-commits">
      {commits.data.map((commit) => (
        <Radio
          key={commit.sha}
          name={name}
          value={commit.sha}
          checked={value === commit.sha}
          onChange={() => onSelect(commit)}
          label={<span className="ui-break">{commit.message}</span>}
          description={
            <>
              <span className="ui-mono">{commit.sha.slice(0, 9)}</span>
              {commit.author && ` · ${commit.author}`}
              {commit.date && (
                <>
                  {' · '}
                  <RelativeTime value={commit.date} />
                </>
              )}
            </>
          }
        />
      ))}
    </Fieldset>
  );
}
