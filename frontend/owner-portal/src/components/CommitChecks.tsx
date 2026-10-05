import { Alert, Hint, Icon } from '@openstack-platform/ui';
import { useQuery } from '@tanstack/react-query';
import type { Configuration, SourceReadOptions } from '../api';
import { GitHubError } from '../utils/github';
import { checkCommit, type CommitCheck } from '../utils/preflight';
import { runtimeNames } from '../utils/runtimeVersions';
import '../pages/app-pages.css';

/** A full SHA in a repository the browser can ask GitHub about. */
export function canCheck(repository: string, sha: string) {
  return !!repository && /^[a-f0-9]{40}$/.test(sha);
}

/** Pre-deploy checks of a commit against the build's rules, from GitHub. */
export function useCommitChecks(
  repository: string,
  sha: string,
  configuration?: Configuration,
  platform?: SourceReadOptions,
) {
  return useQuery({
    queryKey: [
      'commit-checks',
      platform?.scope ?? '',
      platform?.id ?? '',
      platform?.revision ?? 0,
      repository,
      sha,
      configuration ? JSON.stringify(configuration) : '',
    ],
    queryFn: async ({ signal }) => {
      try {
        return await checkCommit(repository, sha, configuration!, signal);
      } catch (error) {
        if (signal.aborted || !platform || !(await platform.service.sourceKey(platform.id)).present)
          throw error;
        return platform.service.checkSourceCommit(platform.id, sha, platform.revision);
      }
    },
    enabled: canCheck(repository, sha) && !!configuration,
    // A commit never changes; the settings are part of the key.
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });
}

export function problems(checks: CommitCheck[] | undefined) {
  return (checks ?? []).filter((check) => check.state === 'problem');
}

/** The problems a check found, as a list; nothing when there are none. */
export function CommitProblems({ checks }: { checks: CommitCheck[] | undefined }) {
  const found = problems(checks);
  if (!found.length) return null;
  return (
    <Alert tone="warning" title="This commit will fail to build">
      <ul className="app-check-list">
        {found.map((check) => (
          <li key={check.id}>{check.problem}</li>
        ))}
      </ul>
    </Alert>
  );
}

/** What the commit asks for, when it was checked: the build picks the exact release. */
function RuntimeVersion({
  checks,
  configuration,
}: {
  checks: CommitCheck[];
  configuration: Configuration;
}) {
  const check = checks.find(
    (item) =>
      (item.id === 'runtime-version' || item.id === 'runtime-default') && item.state === 'ok',
  );
  if (!check) return null;
  return (
    <p className="app-check ui-text-sm ui-text-muted" role="status">
      <Icon name="info" />
      <span>
        {check.id === 'runtime-version'
          ? `This commit asks for ${check.label}. The build picks the exact release.`
          : `This commit doesn’t ask for a ${runtimeNames[configuration.build.runtime]} version, so the build uses the platform’s default.`}
      </span>
    </p>
  );
}

/** One line under the commit field: checking, passed, or what to fix. */
export function CommitChecks({
  repository,
  sha,
  configuration,
  platform,
}: {
  repository: string;
  sha: string;
  configuration: Configuration;
  platform?: SourceReadOptions;
}) {
  const checks = useCommitChecks(repository, sha, configuration, platform);
  if (!canCheck(repository, sha)) return null;
  if (checks.isPending)
    return (
      <p className="app-check ui-text-sm ui-text-muted" role="status">
        Checking this commit…
      </p>
    );
  if (checks.error)
    return (
      <Hint>
        {checks.error instanceof GitHubError && checks.error.problem === 'rate-limited'
          ? 'GitHub’s hourly limit was reached, so this commit wasn’t checked. The build checks it when you deploy.'
          : 'Couldn’t read this commit on GitHub, so it wasn’t checked. The build checks it when you deploy.'}
      </Hint>
    );
  if (problems(checks.data).length) return <CommitProblems checks={checks.data} />;
  const unchecked = checks.data.some((check) => check.state === 'unknown');
  return (
    <>
      <p className="app-check ui-text-sm" role="status">
        <Icon name={unchecked ? 'info' : 'success'} />
        <span className={unchecked ? 'ui-text-muted' : undefined}>
          {unchecked
            ? 'Some files couldn’t be checked. The build checks them when you deploy.'
            : 'This commit has the package.json, scripts and lockfile the build needs.'}
        </span>
      </p>
      <RuntimeVersion checks={checks.data} configuration={configuration} />
    </>
  );
}
