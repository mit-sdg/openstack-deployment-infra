import { Button, Fieldset, Hint, LoadingRows, Radio, RelativeTime } from '@openstack-platform/ui';
import { useMutation, useQuery } from '@tanstack/react-query';
import type { SourceAccess } from '../api';
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
export function useRecentCommits(repository: string, branch: string) {
  return useQuery({
    queryKey: ['github-commits', repository, branch],
    queryFn: ({ signal }) => recentCommits(repository, branch, signal),
    enabled: !!repository && !!branch,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}

/**
 * For a private repository GitHub won't list commits to the browser; the app's
 * deploy key can still read the branch's newest commit on the platform side.
 */
function LatestWithKey({
  branch,
  latest,
  onSelect,
}: {
  branch: string;
  latest: () => Promise<SourceAccess>;
  onSelect: (commit: RecentCommit) => void;
}) {
  const read = useMutation({
    mutationFn: latest,
    onSuccess: (access) => {
      if (access.keyPresent && access.head)
        onSelect({
          sha: access.head,
          message: `Latest commit on ${branch}`,
          author: null,
          date: null,
        });
    },
  });
  const access = read.data;
  return (
    <div className="ui-stack ui-gap-2">
      <div>
        <Button size="sm" loading={read.isPending} onClick={() => read.mutate()}>
          Use the latest commit on {branch}
        </Button>
      </div>
      {(read.error || (access && !(access.keyPresent && access.head))) && (
        <Hint>
          {access && !access.keyPresent
            ? 'This app has no deploy key. Create one in Settings to deploy from a private repository.'
            : 'The deploy key couldn’t read the branch. Check access in Settings.'}
        </Hint>
      )}
    </div>
  );
}

/** Pick one of the newest commits on the branch; a SHA field stays the fallback. */
export function RecentCommits({
  repository,
  branch,
  value,
  onSelect,
  latest,
}: {
  repository: string;
  branch: string;
  value: string;
  onSelect: (commit: RecentCommit) => void;
  /** Reads the branch's newest commit with the app's deploy key. */
  latest?: () => Promise<SourceAccess>;
}) {
  const name = useId();
  const commits = useRecentCommits(repository, branch);
  const legend = `Recent commits on ${branch}`;
  if (commits.isPending)
    return (
      <Fieldset legend={legend}>
        <LoadingRows rows={3} label="Loading recent commits…" />
      </Fieldset>
    );
  if (commits.error) {
    const problem = commits.error instanceof GitHubError ? commits.error.problem : 'unavailable';
    return (
      <>
        <Hint>{problems[problem]}</Hint>
        {problem === 'not-found' && latest && (
          <LatestWithKey branch={branch} latest={latest} onSelect={onSelect} />
        )}
      </>
    );
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
