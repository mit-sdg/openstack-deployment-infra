import { useState } from "react";
import { ActivityRow, Card, type Tone } from "@openstack-platform/ui";
import { Icon, ToneIcon } from "./Icons";
import {
  Badge,
  Time,
  clockFormat,
  parseTime,
  plural,
  sentence,
} from "./presentation";
import type { Issue, ReadySnapshot, Snapshot, Role } from "./snapshot";

export function Skeletons({ count = 3 }: { count?: number }) {
  return (
    <div className="skeleton-rows" aria-hidden="true">
      {Array.from({ length: count }, (_, index) => (
        <div key={index} className="skeleton-block" />
      ))}
    </div>
  );
}
function Stat({
  icon,
  label,
  value,
  of,
  sub,
  tone,
}: {
  icon: string;
  label: string;
  value: string;
  of?: string;
  sub: string;
  tone?: Tone;
}) {
  return (
    <div className="stat">
      <dt>
        <Icon name={icon} />
        {label}
      </dt>
      <dd>
        <span className="stat__value">
          {value}
          {of && <span className="stat__of">{of}</span>}
        </span>
        <span className="stat__sub">
          {tone && <ToneIcon tone={tone} />}
          <span>{sub}</span>
        </span>
      </dd>
    </div>
  );
}
export function Overview({ snapshot }: { snapshot: Snapshot | null }) {
  const ready = snapshot?.state === "ready";
  let metrics;
  if (ready) {
    const {
      roles,
      applications: apps,
      operations,
      backup,
    } = snapshot.summary.counts;
    const failing = snapshot.roles.filter(
      (r) => r.status.tone === "critical",
    ).length;
    const degraded = snapshot.roles.filter(
      (r) => r.status.tone === "warning",
    ).length;
    const unverified = snapshot.roles.filter(
      (r) => r.status.key === "unverified",
    ).length;
    const roleSub =
      [
        failing ? `${failing} failing` : null,
        degraded ? `${degraded} degraded` : null,
        unverified ? `${unverified} unverified` : null,
      ]
        .filter(Boolean)
        .join(" · ") || "healthy";
    const expected = apps.total - apps.stopped;
    const appSub =
      apps.total === 0
        ? "none declared yet"
        : expected === 0
          ? "all stopped"
          : apps.attention
            ? `${apps.attention} need attention`
            : apps.changing
              ? `serving · ${apps.changing} changing`
              : apps.stopped
                ? `serving · ${apps.stopped} stopped`
                : "serving";
    const appTone = apps.attention
      ? snapshot.applications.some((app) => app.status.tone === "critical")
        ? "critical"
        : "warning"
      : apps.changing
        ? "info"
        : undefined;
    const backupValue =
      typeof backup.ageHours === "number"
        ? `${backup.ageHours < 10 ? backup.ageHours.toFixed(1) : Math.round(backup.ageHours)} h`
        : "—";
    const backupSub =
      backup.state === "failed"
        ? "backup check failed"
        : backup.state === "not-run"
          ? "not checked this run"
          : backup.offsite === "failed"
            ? "off-site check failed"
            : typeof backup.ageHours === "number"
              ? backup.offsite === "passed"
                ? "off-site copy verified"
                : "encrypted set"
              : "no health evidence";
    metrics = (
      <>
        <Stat
          icon="i-roles"
          label="Infrastructure roles"
          value={String(roles.healthy)}
          of={`/${roles.total}`}
          sub={roleSub}
          tone={
            failing
              ? "critical"
              : degraded
                ? "warning"
                : unverified
                  ? "neutral"
                  : undefined
          }
        />
        <Stat
          icon="i-apps"
          label="Applications"
          value={String(apps.serving)}
          of={expected > 0 ? `/${expected}` : undefined}
          sub={appSub}
          tone={appTone}
        />
        <Stat
          icon="i-activity"
          label="Operations"
          value={String(operations.running)}
          sub={
            operations.recovery
              ? `${operations.recovery} need recovery`
              : operations.running
                ? "running now"
                : "none running"
          }
          tone={
            operations.recovery
              ? "warning"
              : operations.running
                ? "info"
                : undefined
          }
        />
        <Stat
          icon="i-backup"
          label="Newest backup"
          value={backupValue}
          of={typeof backup.ageHours === "number" ? " ago" : undefined}
          sub={backupSub}
          tone={
            backup.state === "failed" || backup.offsite === "failed"
              ? "warning"
              : backup.state === "not-run"
                ? "neutral"
                : undefined
          }
        />
      </>
    );
  }
  return (
    <Card
      className="overview"
      id="overview"
      data-tone={ready ? snapshot.summary.tone : "pending"}
      aria-labelledby="headline"
    >
      <div className="overview__status">
        <span className="overview__icon" aria-hidden="true">
          {ready ? (
            <ToneIcon tone={snapshot.summary.tone} />
          ) : (
            <span className="spinner" />
          )}
        </span>
        <div className="overview__text">
          <h1 className="overview__headline" id="headline" aria-live="polite">
            {ready ? snapshot.summary.headline : "Collecting platform status…"}
          </h1>
          <p className="overview__detail">
            {ready
              ? snapshot.summary.detail
              : "The first refresh waits for the admin host, OpenStack, and public routes to answer."}
          </p>
        </div>
        {ready && (
          <p className="overview__checked">
            <strong>
              {parseTime(snapshot.generatedAt) === null
                ? "Not checked yet"
                : `Checked ${clockFormat.format(parseTime(snapshot.generatedAt)!)}`}
            </strong>
            {snapshot.refresh?.intervalSeconds
              ? `Refreshes every ${snapshot.refresh.intervalSeconds} s`
              : null}
          </p>
        )}
      </div>
      <dl className="stats">
        {metrics ??
          Array.from({ length: 4 }, (_, i) => (
            <div key={i} className="stat skeleton-block" />
          ))}
      </dl>
    </Card>
  );
}
export function groupIssues(issues: Issue[]) {
  const key = (issue: Issue) =>
    `${issue.tone}|${issue.summary}|${issue.detail}`;
  const groups = new Map<string, Issue[]>();
  for (const issue of issues)
    if (issue.scope === "application")
      groups.set(key(issue), [...(groups.get(key(issue)) ?? []), issue]);
  const emitted = new Set<string>();
  const rows: Issue[] = [];
  for (const issue of issues) {
    const group =
      issue.scope === "application" ? groups.get(key(issue)) : undefined;
    if (!group || group.length < 3) {
      rows.push(issue);
      continue;
    }
    if (emitted.has(key(issue))) continue;
    emitted.add(key(issue));
    rows.push({
      ...issue,
      scope: "applications",
      subject: plural(group.length, "application"),
      detail: `${sentence(issue.detail)} · ${group
        .slice(0, 3)
        .map((x) => x.subject)
        .join(", ")}${group.length > 3 ? ` and ${group.length - 3} more` : ""}`,
    });
  }
  return rows;
}
export function Issues({
  snapshot,
  openApp,
  attention,
}: {
  snapshot: ReadySnapshot | null;
  openApp: (id: string) => void;
  attention: () => void;
}) {
  if (!snapshot?.issues.length) return null;
  return (
    <section
      className="section"
      id="issues-section"
      aria-labelledby="issues-title"
    >
      <Card className="issues">
        <div className="card__header">
          <h2 className="card__title" id="issues-title">
            Needs attention
          </h2>
          <span className="count">{snapshot.issues.length}</span>
        </div>
        <ul className="issue-list">
          {groupIssues(snapshot.issues).map((issue, i) => {
            const action =
              issue.scope === "application"
                ? () => openApp(issue.target)
                : issue.scope === "applications"
                  ? attention
                  : issue.scope === "role"
                    ? () => {
                        const card = document.getElementById(
                          `role-${issue.target}`,
                        );
                        card?.scrollIntoView({
                          behavior: matchMedia(
                            "(prefers-reduced-motion: reduce)",
                          ).matches
                            ? "instant"
                            : "smooth",
                          block: "center",
                        });
                        if (card) {
                          card.dataset.highlight = "true";
                          setTimeout(() => {
                            card.dataset.highlight = "false";
                          }, 2200);
                        }
                      }
                    : undefined;
            return (
              <li
                className="issue"
                key={`${issue.scope}-${issue.target}-${issue.summary}-${i}`}
              >
                <button
                  className="issue__button"
                  type="button"
                  disabled={!action}
                  onClick={action}
                >
                  <ToneIcon tone={issue.tone} />
                  <span>
                    <span className="issue__title">{issue.subject}</span>
                    <span className="issue__subject"> — {issue.summary}</span>
                    {issue.detail && (
                      <span className="issue__detail">
                        {" "}
                        {sentence(issue.detail)}
                      </span>
                    )}
                  </span>
                  {action ? (
                    <span className="issue__go">
                      <Icon name="i-chevron" />
                    </span>
                  ) : (
                    <span />
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      </Card>
    </section>
  );
}
function RoleCard({ role }: { role: Role }) {
  return (
    <article
      className="role-card card"
      id={`role-${role.role}`}
      data-role={role.role}
      data-lifetime={role.lifetime}
      aria-labelledby={`role-${role.role}-name`}
    >
      <div className="role-card__head">
        <span className="role-card__icon" aria-hidden="true">
          <Icon name={`i-${role.role}`} />
        </span>
        <div className="role-card__title">
          <h3 className="role-card__name" id={`role-${role.role}-name`}>
            {role.name}
          </h3>
          <div className="role-card__lifetime">{role.lifetimeLabel}</div>
        </div>
        <Badge status={role.status} />
      </div>
      <p className="role-card__summary">{role.summary}</p>
      <dl className="signals">
        {role.signals.map((signal, i) => (
          <div
            className="signal"
            key={`${signal.label}-${i}`}
            title={sentence(signal.detail) ?? undefined}
          >
            <dt>{signal.label}</dt>
            <dd>
              <span
                className="dot"
                data-tone={signal.tone}
                aria-hidden="true"
              />
              <span className="signal__value">{signal.value}</span>
            </dd>
            {signal.detail && ["critical", "warning"].includes(signal.tone) && (
              <span className="signal__detail">{sentence(signal.detail)}</span>
            )}
          </div>
        ))}
        {role.facts.map((fact, i) => (
          <div className="signal" key={`${fact.label}-${i}`}>
            <dt>{fact.label}</dt>
            <dd>
              <span className="signal__value signal__value--fact">
                {fact.value}
              </span>
            </dd>
          </div>
        ))}
      </dl>
      <div className="role-card__foot">
        {role.image && (role.image.name || role.image.commit) ? (
          <>
            <span
              className="role-card__image mono"
              title={role.image.name ?? ""}
            >
              {role.image.name || "Image"}
            </span>
            {role.image.commit && (
              <span className="chip" title={role.image.commit}>
                <Icon name="i-commit" />
                <span className="mono">{role.image.commit.slice(0, 7)}</span>
              </span>
            )}
            {role.image.selectedAt && (
              <Time value={role.image.selectedAt} prefix="Selected " />
            )}
          </>
        ) : (
          <span>No image selection recorded</span>
        )}
      </div>
    </article>
  );
}
export function Roles({ snapshot }: { snapshot: ReadySnapshot | null }) {
  return (
    <section
      className="section"
      id="roles-section"
      aria-labelledby="roles-title"
    >
      <div className="section__header">
        <h2 className="section__title" id="roles-title">
          Infrastructure roles
        </h2>
        <p className="section__note">
          {snapshot
            ? `${snapshot.summary.counts.roles.healthy} of ${snapshot.summary.counts.roles.total} healthy · persistent hosts, replaceable workers, single-use builders`
            : "Five machine roles from the platform contract"}
        </p>
      </div>
      <div className="roles">
        {snapshot
          ? snapshot.roles.map((role) => (
              <RoleCard key={role.role} role={role} />
            ))
          : Array.from({ length: 5 }, (_, i) => (
              <div key={i} className="role-card card skeleton-block" />
            ))}
      </div>
    </section>
  );
}
export function StaleNotice({ snapshot }: { snapshot: ReadySnapshot }) {
  const source = snapshot.sources.find((item) => item.key === "controller");
  return source && !source.ok ? (
    <div className="stale-note" role="note">
      <ToneIcon tone="warning" />
      <span>
        {source.observedAt && (
          <>
            Showing controller records from <Time value={source.observedAt} />
            .{" "}
          </>
        )}
        {sentence(source.error) ?? "The latest controller read failed."}.
      </span>
    </div>
  ) : null;
}
export function Operations({
  snapshot,
  openApp,
}: {
  snapshot: ReadySnapshot | null;
  openApp: (id: string) => void;
}) {
  const [showAll, setShowAll] = useState(false);
  const operations = snapshot?.operations ?? [];
  const running = operations.filter(
      (item) => item.status.key === "running",
    ).length,
    recovery = operations.filter(
      (item) => item.status.key === "recovery_required",
    ).length;
  const notes = [
    running ? `${running} running` : null,
    recovery ? `${recovery} need recovery` : null,
  ].filter(Boolean);
  if (!notes.length) notes.push("Most recent first");
  if (snapshot?.operationsTruncated) notes.push("older operations not loaded");
  return (
    <section
      className="section"
      id="operations-section"
      aria-labelledby="operations-title"
    >
      <Card>
        <div className="card__header">
          <h2 className="card__title" id="operations-title">
            Operations
          </h2>
          <span className="card__note">
            {snapshot ? notes.join(" · ") : ""}
          </span>
        </div>
        {!snapshot ? (
          <Skeletons />
        ) : (
          <>
            <StaleNotice snapshot={snapshot} />
            {!operations.length ? (
              <div className="empty empty--compact">
                <Icon name="i-activity" />
                <div className="empty__title">No operations recorded</div>
                <div>
                  Deployments, storage changes, and host operations appear here.
                </div>
              </div>
            ) : (
              <>
                <ul className="op-list">
                  {(showAll ? operations : operations.slice(0, 8)).map(
                    (operation) => (
                      <ActivityRow className="op" key={operation.id}>
                        <ToneIcon tone={operation.status.tone} />
                        <div>
                          <div>
                            <span className="op__title">{operation.label}</span>
                            <span className="op__subject"> · </span>
                            {operation.applicationId ? (
                              <button
                                className="op__subject--link"
                                type="button"
                                onClick={() =>
                                  openApp(operation.applicationId!)
                                }
                              >
                                {operation.subject}
                              </button>
                            ) : (
                              <span className="op__subject">
                                {operation.subject}
                              </span>
                            )}
                          </div>
                          <div className="op__meta">
                            {[operation.status.label, operation.phase]
                              .filter(Boolean)
                              .join(" · ")}
                            {operation.startedAt && (
                              <>
                                {" "}
                                ·{" "}
                                <Time
                                  value={operation.startedAt}
                                  prefix="started "
                                />
                              </>
                            )}
                          </div>
                          {operation.error &&
                            operation.status.key !== "succeeded" && (
                              <div className="op__error">{operation.error}</div>
                            )}
                        </div>
                        <span className="op__time">
                          <Time value={operation.updatedAt} />
                        </span>
                      </ActivityRow>
                    ),
                  )}
                </ul>
                {operations.length > 8 && (
                  <button
                    type="button"
                    className="list-more"
                    onClick={() => setShowAll(!showAll)}
                  >
                    {showAll ? "Show fewer" : `Show all ${operations.length}`}
                  </button>
                )}
              </>
            )}
          </>
        )}
      </Card>
    </section>
  );
}
export function Checks({ snapshot }: { snapshot: ReadySnapshot | null }) {
  const checks = snapshot?.checks;
  const labels: Record<string, string> = {
    passed: "Passed",
    failed: "Failed",
    "not-run": "Skipped",
    unavailable: "Unavailable",
  };
  return (
    <section
      className="section"
      id="checks-section"
      aria-labelledby="checks-title"
    >
      <Card>
        <div className="card__header">
          <h2 className="card__title" id="checks-title">
            Platform checks
          </h2>
          <span className="card__note">
            {checks?.available && checks.checkedAt ? (
              <>
                Health timer <Time value={checks.checkedAt} />
                {checks.stale && <span className="stale-chip">Stale</span>}
              </>
            ) : snapshot ? (
              "No snapshot yet"
            ) : (
              ""
            )}
          </span>
        </div>
        <ul className="check-list">
          {!snapshot ? (
            <li>
              <Skeletons />
            </li>
          ) : !checks?.available ? (
            <li className="empty empty--compact">
              <Icon name="i-shield" />
              <div className="empty__title">No platform-health snapshot</div>
              <div>
                {sentence(
                  snapshot.sources.find((source) => source.key === "health")
                    ?.error,
                ) || "The admin health timer has not reported yet."}
              </div>
            </li>
          ) : (
            checks.items.map((check, i) => (
              <li className="check" key={`${check.label}-${i}`}>
                <ToneIcon tone={check.tone} />
                <div>
                  <div className="check__title">{check.label}</div>
                  <div className="check__detail">{sentence(check.detail)}</div>
                </div>
                <span className="check__state">
                  {labels[check.state] ?? check.state}
                </span>
              </li>
            ))
          )}
        </ul>
      </Card>
    </section>
  );
}
export function Footer({ snapshot }: { snapshot: Snapshot | null }) {
  return (
    <div className="footer__inner">
      <span>Read-only view. Nothing on this page can change the platform.</span>
      {snapshot?.state === "ready" && (
        <ul className="sources" aria-label="Data sources">
          {snapshot.sources.map((source) => (
            <li
              className="source"
              key={source.key}
              title={
                sentence(source.error) ??
                (source.observedAt
                  ? new Date(source.observedAt).toLocaleString()
                  : "")
              }
            >
              <span
                className="dot"
                data-tone={source.ok ? "good" : "warning"}
                aria-hidden="true"
              />
              <span>{source.label}</span>
              {source.observedAt ? (
                <Time value={source.observedAt} />
              ) : (
                <span>never</span>
              )}
            </li>
          ))}
        </ul>
      )}
      {snapshot?.platform.release && (
        <span className="footer__release" title={snapshot.platform.release}>
          Release{" "}
          <span className="mono">{snapshot.platform.release.slice(0, 7)}</span>
        </span>
      )}
    </div>
  );
}
