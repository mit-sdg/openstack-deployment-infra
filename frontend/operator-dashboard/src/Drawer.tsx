import { Fragment, useEffect, useRef, useState, type ReactNode } from "react";
import { Icon, ToneIcon } from "./Icons";
import {
  Badge,
  ExternalLink,
  HistoryBars,
  Time,
  formatBytes,
  formatCpu,
  formatMemory,
  hostOf,
  numberFormat,
  sentence,
} from "./presentation";
import { routeText } from "./Applications";
import type { Application, Storage } from "./snapshot";

function Kv({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="kv">
      {rows
        .filter(([, value]) => value !== null && value !== undefined)
        .map(([label, value]) => (
          <Fragment key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </Fragment>
        ))}
    </dl>
  );
}
function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="dsec">
      <h3 className="dsec__title">{title}</h3>
      {children}
    </section>
  );
}
function Copy({
  value,
  label,
  notify,
}: {
  value: string;
  label: string;
  notify: (message: string) => void;
}) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  return (
    <button
      type="button"
      className="copy"
      title={`Copy ${label}`}
      aria-label={`Copy ${label}`}
      data-copied={copied}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setCopied(true);
          notify(`Copied ${label}`);
          clearTimeout(timer.current);
          timer.current = setTimeout(() => setCopied(false), 1600);
        } catch {
          notify("Copying is unavailable in this browser");
        }
      }}
    >
      <Icon name={copied ? "i-check" : "i-copy"} />
    </button>
  );
}
function Identifier({
  value,
  label,
  notify,
}: {
  value: string | null;
  label: string;
  notify: (message: string) => void;
}) {
  return value ? (
    <>
      <span className="mono" title={value}>
        {value}
      </span>
      <Copy value={value} label={label} notify={notify} />
    </>
  ) : (
    <span className="muted">—</span>
  );
}
function Resource({ resource }: { resource: Storage }) {
  const labels: Record<string, [string, (value: number) => string]> = {
    postgresConnections: ["Connections", (value) => numberFormat.format(value)],
    measuredTargetBytes: [
      resource.type === "postgres" ? "Size target" : "Size limit",
      formatBytes,
    ],
    s3Bytes: ["Size limit", formatBytes],
    s3Objects: ["Objects", (value) => numberFormat.format(value)],
  };
  const quotas = Object.entries(resource.quotas)
    .filter(([key]) => key in labels)
    .map(([key, value]) => `${labels[key][0]} ${labels[key][1](value)}`)
    .join(" · ");
  return (
    <div className="resource">
      <div className="resource__head">
        <span className="chip">{resource.typeLabel}</span>
        <span className="resource__name">
          {resource.label || resource.name}
        </span>
        <Badge status={resource.status} />
      </div>
      <div className="resource__meta">
        {resource.usage.measuredAt ? (
          <>
            <span>
              Used{" "}
              {resource.usage.usedBytes === null
                ? "—"
                : `${formatBytes(resource.usage.usedBytes)}${(resource.quotas.s3Bytes ?? resource.quotas.measuredTargetBytes) ? ` of ${formatBytes(resource.quotas.s3Bytes ?? resource.quotas.measuredTargetBytes)}` : ""}`}
            </span>
            {resource.usage.objectCount !== null && (
              <span>
                {" "}
                · {numberFormat.format(resource.usage.objectCount)}
                {resource.quotas.s3Objects !== undefined &&
                  ` of ${numberFormat.format(resource.quotas.s3Objects)}`}{" "}
                objects
              </span>
            )}
            {resource.usage.currentConnections !== null && (
              <span>
                {" "}
                · {numberFormat.format(resource.usage.currentConnections)}
                {resource.quotas.postgresConnections !== undefined &&
                  ` of ${numberFormat.format(resource.quotas.postgresConnections)}`}{" "}
                connections
              </span>
            )}{" "}
            · <Time value={resource.usage.measuredAt} prefix="Updated " />
            {resource.usage.stale && " · Stale"}
          </>
        ) : (
          "Usage not measured yet"
        )}
      </div>
      {resource.writeBlock.blocked && (
        <div className="resource__meta" role="status">
          Writes paused: size limit exceeded. Reads and deletes remain
          available.
        </div>
      )}
      <div className="resource__meta">
        {quotas || "No quota recorded"} ·{" "}
        {resource.lastVerifiedAt ? (
          <Time value={resource.lastVerifiedAt} prefix="Verified " />
        ) : (
          "Never verified"
        )}
      </div>
    </div>
  );
}
export function DrawerContent({
  app,
  notify,
}: {
  app: Application;
  notify: (message: string) => void;
}) {
  const { route, deployment, operation } = app;
  const detail =
    operation && ["changing", "needs-recovery"].includes(app.status.key)
      ? `${operation.label} · ${operation.status.label}${operation.phase ? ` · ${operation.phase}` : ""}`
      : (app.recoveryNote ?? route?.detail);
  return (
    <>
      <section className="dsec">
        <div className="dsec__lead">
          <ToneIcon tone={app.status.tone} />
          <div>
            <strong>{app.status.label}</strong>
            {detail && <span>{sentence(detail)}</span>}
          </div>
        </div>
      </section>
      <Section title="Health check">
        <Kv
          rows={
            route && route.outcome !== "not-checked"
              ? [
                  [
                    "Endpoint",
                    <ExternalLink
                      url={route.url}
                      label={route.url.replace(/^https:\/\//, "")}
                    />,
                  ],
                  [
                    "Response",
                    <span className="health-value">{routeText(route)}</span>,
                  ],
                  ["Checked", <Time value={route.checkedAt} />],
                  [
                    "Recent checks",
                    <HistoryBars checks={app.checks} label="Health checks" />,
                  ],
                ]
              : [["Result", route?.detail ?? "Not checked"]]
          }
        />
      </Section>
      {operation && (
        <Section title="Operation in progress">
          <Kv
            rows={[
              ["Kind", operation.label],
              ["Status", <Badge status={operation.status} />],
              ["Phase", operation.phase ?? "—"],
              ["Started", <Time value={operation.startedAt} />],
              ["Updated", <Time value={operation.updatedAt} />],
              [
                "Deadline",
                operation.deadlineAt ? (
                  <Time value={operation.deadlineAt} />
                ) : null,
              ],
              [
                "Operation",
                <Identifier
                  value={operation.id}
                  label="operation ID"
                  notify={notify}
                />,
              ],
              ["Error", operation.error],
            ]}
          />
        </Section>
      )}
      {deployment && (
        <Section title="Accepted deployment">
          <Kv
            rows={[
              [
                "Commit",
                deployment.commit ? (
                  <>
                    {deployment.commitUrl ? (
                      <ExternalLink
                        url={deployment.commitUrl}
                        label={deployment.commit.slice(0, 12)}
                        className="mono"
                      />
                    ) : (
                      <span className="mono">
                        {deployment.commit.slice(0, 12)}
                      </span>
                    )}
                    <Copy
                      value={deployment.commit}
                      label="commit"
                      notify={notify}
                    />
                  </>
                ) : (
                  <span className="muted">—</span>
                ),
              ],
              ["Branch", deployment.ref],
              [
                "Repository",
                deployment.repository ? (
                  <ExternalLink
                    url={deployment.repository}
                    label={deployment.repository.replace(/^https:\/\//, "")}
                  />
                ) : null,
              ],
              [
                "Runtime",
                `${deployment.runtime ?? "Unknown"}${deployment.port ? ` · port ${deployment.port}` : ""}`,
              ],
              [
                "Health path",
                deployment.healthPath ? (
                  <span className="mono">{deployment.healthPath}</span>
                ) : null,
              ],
              ["Accepted", <Time value={deployment.acceptedAt} />],
              [
                "Last healthy",
                deployment.lastHealthyAt ? (
                  <Time value={deployment.lastHealthyAt} />
                ) : null,
              ],
              [
                "Configuration",
                typeof deployment.configurationRevision === "number"
                  ? `Revision ${deployment.configurationRevision}`
                  : null,
              ],
              [
                "Image",
                deployment.imageDigest ? (
                  <>
                    <span className="mono" title={deployment.imageDigest}>
                      …{deployment.imageDigest.slice(-19)}
                    </span>
                    <Copy
                      value={deployment.imageDigest}
                      label="image digest"
                      notify={notify}
                    />
                  </>
                ) : null,
              ],
              [
                "Deployment",
                <Identifier
                  value={deployment.id}
                  label="deployment ID"
                  notify={notify}
                />,
              ],
            ]}
          />
        </Section>
      )}
      {app.attempts.length > 0 && (
        <Section title="Recent deployments">
          <ul className="attempts">
            {app.attempts.map((attempt) => (
              <li className="attempt" key={attempt.id}>
                <ToneIcon tone={attempt.status.tone} />
                <div className="attempt__main">
                  <div className="cell-line">
                    <strong>{attempt.status.label}</strong>
                    {attempt.commit && (
                      <span className="mono muted">
                        {attempt.commit.slice(0, 7)}
                      </span>
                    )}
                    {attempt.ref && (
                      <span className="ref">
                        <Icon name="i-branch" />
                        {attempt.ref}
                      </span>
                    )}
                  </div>
                  {attempt.error && (
                    <div className="attempt__error">{attempt.error}</div>
                  )}
                </div>
                <span className="op__time">
                  <Time value={attempt.requestedAt} />
                </span>
              </li>
            ))}
          </ul>
        </Section>
      )}
      <Section title="Sizing">
        <Kv
          rows={[
            [
              "Worker flavor",
              app.sizing.flavor ? (
                <span className="mono">{app.sizing.flavor}</span>
              ) : (
                "Platform default"
              ),
            ],
            ["CPU", formatCpu(app.sizing.cpuMHz) ?? "Platform default"],
            [
              "Memory",
              formatMemory(app.sizing.memoryMiB) ?? "Platform default",
            ],
          ]}
        />
      </Section>
      <Section title="Managed storage">
        {app.storage.length ? (
          app.storage.map((resource) => (
            <Resource key={resource.id} resource={resource} />
          ))
        ) : (
          <p className="muted">
            No managed PostgreSQL, MongoDB, or S3 resources.
          </p>
        )}
      </Section>
      <Section title="Identifiers">
        <Kv
          rows={[
            [
              "Application",
              <Identifier
                value={app.id}
                label="application ID"
                notify={notify}
              />,
            ],
            ["Created", <Time value={app.createdAt} />],
            ["Updated", <Time value={app.updatedAt} />],
            ["Deleted", app.deletedAt ? <Time value={app.deletedAt} /> : null],
          ]}
        />
      </Section>
    </>
  );
}
export function Drawer({
  app,
  close,
  notify,
}: {
  app: Application | null;
  close: () => void;
  notify: (message: string) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null),
    closeButton = useRef<HTMLButtonElement>(null);
  const appId = app?.id;
  useEffect(() => {
    const element = dialog.current!;
    if (!appId) {
      if (element.open) element.close();
      return;
    }
    const trigger =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    if (!element.open) {
      element.showModal();
      element.querySelector(".drawer__body")?.scrollTo(0, 0);
      closeButton.current?.focus();
    }
    return () => {
      if (element.open) element.close();
      if (trigger?.isConnected) trigger.focus({ preventScroll: true });
      else
        document
          .querySelector<HTMLButtonElement>(
            `[data-app-id="${CSS.escape(appId)}"]`,
          )
          ?.focus({ preventScroll: true });
    };
  }, [appId]);
  return (
    <dialog
      ref={dialog}
      className="drawer"
      aria-labelledby="drawer-title"
      onClose={close}
      onClick={(event) => {
        if (event.target === dialog.current) dialog.current?.close();
      }}
    >
      <div className="drawer__panel">
        <header className="drawer__header">
          <div className="drawer__heading">
            <h2 className="drawer__title" id="drawer-title">
              {app?.slug}
            </h2>
            <div className="drawer__subtitle">
              {app && (
                <>
                  <Badge status={app.status} />
                  {app.url ? (
                    <ExternalLink url={app.url} label={hostOf(app.url)} />
                  ) : (
                    <span className="muted">No public URL</span>
                  )}
                </>
              )}
            </div>
          </div>
          <button
            ref={closeButton}
            type="button"
            className="icon-button"
            aria-label="Close details"
            onClick={() => dialog.current?.close()}
          >
            <Icon name="i-close" />
          </button>
        </header>
        <div className="drawer__body">
          {app && <DrawerContent app={app} notify={notify} />}
        </div>
      </div>
    </dialog>
  );
}
