import { useRef, useState } from "react";
import { LegacyCard as Card } from "@openstack-platform/ui";
import { Icon } from "./Icons";
import {
  Badge,
  ExternalLink,
  HistoryBars,
  Time,
  formatCpu,
  formatMemory,
  hostOf,
  numberFormat,
  parseTime,
} from "./presentation";
import { Skeletons, StaleNotice } from "./Sections";
import type { Application, ReadySnapshot } from "./snapshot";

export type Filter =
  "all" | "attention" | "serving" | "changing" | "stopped" | "deleted";
export type Sort = "status" | "name" | "deployed" | "latency";
export const filters: {
  key: Filter;
  label: string;
  test: (app: Application) => boolean;
  optional?: boolean;
}[] = [
  { key: "all", label: "All", test: (app) => !app.deletedAt },
  {
    key: "attention",
    label: "Needs attention",
    test: (app) =>
      !app.deletedAt && ["critical", "warning"].includes(app.status.tone),
  },
  {
    key: "serving",
    label: "Serving",
    test: (app) => app.status.key === "serving",
  },
  {
    key: "changing",
    label: "Changing",
    test: (app) => app.status.tone === "info",
    optional: true,
  },
  {
    key: "stopped",
    label: "Stopped",
    test: (app) => app.status.key === "stopped",
  },
  {
    key: "deleted",
    label: "Deleted",
    test: (app) => Boolean(app.deletedAt),
    optional: true,
  },
];
const rank: Record<string, number> = {
  failing: 0,
  "needs-recovery": 1,
  unverified: 2,
  "not-deployed": 3,
  changing: 4,
  unknown: 5,
  serving: 6,
  stopped: 7,
  deleted: 8,
};
export function matchesQuery(app: Application, query: string) {
  return (
    !query ||
    [
      app.slug,
      app.url,
      app.id,
      app.deployment?.id,
      app.deployment?.commit,
      app.deployment?.ref,
      app.status.label,
    ].some(
      (value) =>
        typeof value === "string" && value.toLowerCase().includes(query),
    )
  );
}
export function compareApps(left: Application, right: Application, sort: Sort) {
  if (sort === "name") return left.slug.localeCompare(right.slug);
  if (sort === "deployed")
    return (
      (parseTime(right.deployment?.acceptedAt) ?? -Infinity) -
        (parseTime(left.deployment?.acceptedAt) ?? -Infinity) ||
      left.slug.localeCompare(right.slug)
    );
  if (sort === "latency")
    return (
      (right.route?.latencyMs ?? -1) - (left.route?.latencyMs ?? -1) ||
      left.slug.localeCompare(right.slug)
    );
  return (
    (rank[left.status.key] ?? rank.unknown) -
      (rank[right.status.key] ?? rank.unknown) ||
    left.slug.localeCompare(right.slug)
  );
}
export function routeText(route: Application["route"]) {
  if (!route || route.outcome === "not-checked") return null;
  return `${route.summary}${typeof route.latencyMs === "number" ? ` · ${numberFormat.format(route.latencyMs)} ms` : ""}`;
}
function HealthCell({ app }: { app: Application }) {
  if (routeText(app.route) === null)
    return (
      <div title={app.route?.detail}>
        <div className="cell-line muted">Not checked</div>
      </div>
    );
  return (
    <div title={app.route?.detail}>
      <div className="cell-line">
        <span className="health-value">{app.route?.summary}</span>
        {typeof app.route?.latencyMs === "number" && (
          <span className="health-latency">
            {numberFormat.format(app.route.latencyMs)} ms
          </span>
        )}
      </div>
      <HistoryBars checks={app.checks} label="Health checks" />
    </div>
  );
}
function DeployCell({ app }: { app: Application }) {
  const deployment = app.deployment,
    latest = app.attempts[0];
  if (!deployment)
    return (
      <div>
        <div className="cell-line muted">
          {latest ? latest.status.label : "No deployment"}
        </div>
        {latest && (
          <div className="cell-sub">
            <Time value={latest.requestedAt} prefix="Requested " />
          </div>
        )}
      </div>
    );
  return (
    <div>
      <div className="cell-line">
        <span className="mono" title={deployment.commit ?? ""}>
          {deployment.commit?.slice(0, 7) ?? "—"}
        </span>
        {deployment.ref && (
          <span className="ref" title={deployment.ref}>
            <Icon name="i-branch" />
            <span>{deployment.ref}</span>
          </span>
        )}
      </div>
      <div className="cell-sub">
        <Time value={deployment.acceptedAt} prefix="Accepted " />
      </div>
    </div>
  );
}
function ResourceCell({ app }: { app: Application }) {
  const size = [
    formatCpu(app.sizing.cpuMHz),
    formatMemory(app.sizing.memoryMiB),
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <div>
      <div className="cell-line">
        {app.deployment?.runtime && (
          <span className="chip">{app.deployment.runtime}</span>
        )}
        <span className={size ? "" : "muted"}>{size || "Default size"}</span>
      </div>
      {app.storage.length > 0 && (
        <div className="storage-tags">
          {app.storage.map((resource) => (
            <span
              className="chip"
              key={resource.id}
              title={`${resource.typeLabel} · ${resource.label || resource.name} · ${resource.status.label}`}
            >
              {resource.typeLabel}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
export function Applications({
  snapshot,
  filter,
  setFilter,
  openApp,
}: {
  snapshot: ReadySnapshot | null;
  filter: Filter;
  setFilter: (filter: Filter) => void;
  openApp: (id: string) => void;
}) {
  const [query, setQuery] = useState(""),
    [sort, setSort] = useState<Sort>("status");
  const search = useRef<HTMLInputElement>(null);
  const apps = snapshot?.applications ?? [];
  const active = filters.find((item) => item.key === filter) ?? filters[0];
  const rows = apps
    .filter(
      (app) =>
        active.test(app) && matchesQuery(app, query.trim().toLowerCase()),
    )
    .sort((a, b) => compareApps(a, b, sort));
  return (
    <section
      className="section"
      id="applications-section"
      aria-labelledby="applications-title"
    >
      <div className="section__header">
        <h2 className="section__title" id="applications-title">
          Applications{" "}
          <span className="count">
            {snapshot ? apps.filter((app) => !app.deletedAt).length : ""}
          </span>
        </h2>
        <p className="section__note">
          Public routes checked against each accepted deployment
        </p>
      </div>
      <Card className="apps">
        <div className="toolbar">
          <label className="search">
            <Icon name="i-search" />
            <span className="visually-hidden">Search applications</span>
            <input
              ref={search}
              type="search"
              placeholder="Search applications"
              title="Search by name, URL, commit, deployment, or application ID"
              autoComplete="off"
              spellCheck={false}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <div
            className="segmented"
            role="group"
            aria-label="Filter applications"
          >
            {filters.map((item) => {
              const count = apps.filter(item.test).length;
              return (
                <button
                  type="button"
                  key={item.key}
                  hidden={item.optional && count === 0 && filter !== item.key}
                  aria-pressed={filter === item.key}
                  onClick={() => setFilter(item.key)}
                >
                  <span>{item.label}</span>
                  <span className="segmented__count">
                    {snapshot ? count : ""}
                  </span>
                </button>
              );
            })}
          </div>
          <label className="select">
            <span className="visually-hidden">Sort applications</span>
            <select
              value={sort}
              onChange={(event) => setSort(event.target.value as Sort)}
            >
              <option value="status">Sort: Status</option>
              <option value="name">Sort: Name</option>
              <option value="deployed">Sort: Recently deployed</option>
              <option value="latency">Sort: Response time</option>
            </select>
          </label>
        </div>
        <div className="apps__body">
          {!snapshot ? (
            <Skeletons />
          ) : (
            <>
              <StaleNotice snapshot={snapshot} />
              {!apps.length ? (
                <div className="empty">
                  <Icon name="i-inbox" />
                  <div className="empty__title">No applications yet</div>
                  <div>
                    Applications appear here after they are declared through the
                    controller.
                  </div>
                </div>
              ) : !rows.length ? (
                <div className="empty">
                  <Icon name="i-search" />
                  <div className="empty__title">No applications match</div>
                  <div>Try another search or filter.</div>
                  <button
                    type="button"
                    className="empty__action"
                    onClick={() => {
                      setQuery("");
                      setFilter("all");
                      search.current?.focus();
                    }}
                  >
                    Clear filters
                  </button>
                </div>
              ) : (
                <table className="app-table" role="table">
                  <caption className="visually-hidden">
                    Applications and their latest public health checks
                  </caption>
                  <thead role="rowgroup">
                    <tr role="row">
                      {[
                        "Application",
                        "Status",
                        "Health check",
                        "Deployment",
                        "Resources",
                        "Details",
                      ].map((name, i) => (
                        <th
                          key={name}
                          className={
                            [
                              "col-app",
                              "col-status",
                              "col-health",
                              "col-deploy",
                              "col-resources",
                              "col-go",
                            ][i]
                          }
                          scope="col"
                          role="columnheader"
                        >
                          {i === 5 ? (
                            <span className="visually-hidden">{name}</span>
                          ) : (
                            name
                          )}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody role="rowgroup">
                    {rows.map((app) => (
                      <tr
                        role="row"
                        key={app.id}
                        onClick={() => openApp(app.id)}
                      >
                        <td className="cell-app" role="cell">
                          <button
                            className="app-name"
                            type="button"
                            data-app-id={app.id}
                            aria-label={`${app.slug}, ${app.status.label}. Open details`}
                            onClick={(event) => {
                              event.stopPropagation();
                              openApp(app.id);
                            }}
                          >
                            {app.slug}
                          </button>
                          {app.url ? (
                            <ExternalLink
                              url={app.url}
                              label={hostOf(app.url)}
                              className="app-host"
                            />
                          ) : (
                            <span
                              className="app-host"
                              onClick={(event) => event.stopPropagation()}
                            >
                              No public URL
                            </span>
                          )}
                        </td>
                        <td className="cell-status" role="cell">
                          <Badge status={app.status} />
                        </td>
                        <td className="cell-health" role="cell">
                          <HealthCell app={app} />
                        </td>
                        <td className="cell-deploy" role="cell">
                          <DeployCell app={app} />
                        </td>
                        <td className="cell-resources" role="cell">
                          <ResourceCell app={app} />
                        </td>
                        <td
                          className="cell-go go"
                          role="cell"
                          aria-hidden="true"
                        >
                          <Icon name="i-chevron" />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          )}
        </div>
      </Card>
    </section>
  );
}
