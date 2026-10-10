import { useCallback, useEffect, useState } from "react";
import { Mark, ShellFrame, ThemeButton } from "@openstack-platform/ui";
import { Applications, type Filter } from "./Applications";
import { StorageHost } from "./StorageHost";
import { Drawer } from "./Drawer";
import { Icon, Sprite } from "./Icons";
import {
  Checks,
  Footer,
  Issues,
  Operations,
  Overview,
  Roles,
} from "./Sections";
import { absolute, clockFormat, parseTime, relative } from "./presentation";
import { useNow, useSnapshot } from "./useSnapshot";

export function App() {
  const { snapshot, connected, notice, noticeSequence, poller } = useSnapshot();
  const now = useNow();
  const [filter, setFilter] = useState<Filter>("all"),
    [openId, setOpenId] = useState<string | null>(null),
    [toast, setToast] = useState("");
  const ready = snapshot?.state === "ready" ? snapshot : null;
  const app = ready?.applications.find((item) => item.id === openId) ?? null;
  const close = useCallback(() => setOpenId(null), []);
  const notify = useCallback(
    (message: string) => poller.notice(message),
    [poller],
  );
  useEffect(() => {
    if (ready && openId && !app) setOpenId(null);
  }, [ready, openId, app]);
  useEffect(() => {
    setToast(notice);
    const timer = setTimeout(() => setToast(""), 2600);
    return () => clearTimeout(timer);
  }, [notice, noticeSequence]);
  useEffect(() => {
    document.title = ready
      ? `${ready.summary.headline} · ${ready.platform.name}`
      : "Platform status";
  }, [ready?.summary.headline, ready?.platform.name]);
  const busy = poller.refreshBusy(now),
    platform = snapshot?.platform;
  const freshness = !ready
    ? busy
      ? "Collecting…"
      : ""
    : busy
      ? "Refreshing…"
      : `Updated ${relative(ready.generatedAt, now)}`;
  const connection = connected ? null : (
    <div className="banner" role="status">
      <span className="dot" data-tone="warning" />
      <span>
        {ready
          ? `Lost contact with the dashboard service. Showing the snapshot from ${clockFormat.format(parseTime(ready.generatedAt)!)}. Retrying…`
          : "Cannot reach the dashboard service. Retrying…"}
      </span>
    </div>
  );
  return (
    <>
      <Sprite />
      <ShellFrame
        brand={
          <div className="brand">
            <span className="brand__mark" aria-hidden="true">
              <Mark />
            </span>
            <div className="brand__text">
              <span className="brand__name">
                {platform?.name || "Platform status"}
              </span>
              <span className="brand__meta">
                {[platform?.domain, platform?.region]
                  .filter(Boolean)
                  .join(" · ") || "Operator dashboard"}
              </span>
            </div>
          </div>
        }
        actions={
          <>
            <span
              className="readonly"
              title="This view cannot change the platform"
            >
              <Icon name="i-lock" />
              <span>Read-only</span>
            </span>
            <span
              className="freshness"
              title={
                ready
                  ? `Snapshot generated ${absolute(ready.generatedAt)}`
                  : undefined
              }
            >
              {freshness}
            </span>
            <button
              className="icon-button"
              type="button"
              aria-label="Refresh now"
              title={busy ? "Refreshing…" : "Refresh now"}
              aria-busy={busy}
              onClick={() => {
                void poller.requestRefresh();
              }}
            >
              <Icon name="i-refresh" />
            </button>
            <ThemeButton
              storageKey="platform-dashboard-theme"
              compact
              label={(theme) =>
                `Color theme: ${theme === "system" ? "match system" : theme}`
              }
            />
          </>
        }
        beforeMain={connection}
        footer={<Footer snapshot={snapshot} />}
        afterMain={
          <>
            <Drawer app={app} close={close} notify={notify} />
            <div
              className="toast"
              role="status"
              aria-live="polite"
              hidden={!toast}
            >
              {toast}
            </div>
          </>
        }
      >
        <div data-disconnected={!connected && Boolean(snapshot)}>
          <Overview snapshot={snapshot} />
          <Issues
            snapshot={ready}
            openApp={setOpenId}
            attention={() => {
              setFilter("attention");
              document.getElementById("applications-section")?.scrollIntoView({
                behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
                  ? "instant"
                  : "smooth",
                block: "start",
              });
            }}
          />
          <Roles snapshot={ready} />
          <StorageHost host={ready?.storageHost ?? null} />
          <Applications
            snapshot={ready}
            filter={filter}
            setFilter={setFilter}
            openApp={setOpenId}
          />
          <div className="split">
            <Operations snapshot={ready} openApp={setOpenId} />
            <Checks snapshot={ready} />
          </div>
        </div>
      </ShellFrame>
    </>
  );
}
