import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SnapshotPoller } from "./polling";
import { snapshot } from "./test-fixtures";

const response = (data: unknown, status = 200, etag = '"fixture"') =>
  new Response(status === 304 ? null : JSON.stringify(data), {
    status,
    headers: { ETag: etag },
  });
let poller: SnapshotPoller;
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-01T12:00:00Z"));
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  poller = new SnapshotPoller();
});
afterEach(() => {
  poller.stop();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
async function settle() {
  await vi.advanceTimersByTimeAsync(0);
}
describe("snapshot polling contract", () => {
  it("starts immediately, revalidates at 10 seconds, and retains data on 304", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response(snapshot()))
      .mockResolvedValue(response(null, 304));
    vi.stubGlobal("fetch", fetch);
    poller.start();
    await settle();
    const first = poller.getState().snapshot;
    await vi.advanceTimersByTimeAsync(9999);
    expect(fetch).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[1][1]).toMatchObject({
      headers: { "If-None-Match": '"fixture"' },
      cache: "no-store",
      credentials: "same-origin",
    });
    expect(poller.getState().snapshot).toBe(first);
    expect(poller.getState().connected).toBe(true);
  });
  it("uses 2 seconds for pending or disconnected and recovers cached data on 304", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response(snapshot()))
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue(response(null, 304));
    vi.stubGlobal("fetch", fetch);
    poller.start();
    await settle();
    await vi.advanceTimersByTimeAsync(10000);
    expect(poller.getState().connected).toBe(false);
    await vi.advanceTimersByTimeAsync(1999);
    expect(fetch).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(poller.getState().connected).toBe(true);
    expect(poller.getState().snapshot?.state).toBe("ready");
    expect(poller.nextDelay()).toBe(10000);
  });
  it("polls every 2 seconds during initial collection", async () => {
    const fetch = vi.fn().mockResolvedValue(
      response({
        schemaVersion: 1,
        state: "pending",
        platform: snapshot().platform,
      }),
    );
    vi.stubGlobal("fetch", fetch);
    poller.start();
    await settle();
    expect(poller.refreshBusy()).toBe(true);
    await vi.advanceTimersByTimeAsync(2000);
    expect(fetch).toHaveBeenCalledTimes(2);
  });
  it("polls hidden pages every 60 seconds and immediately when visible again", async () => {
    const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    const fetch = vi.fn().mockResolvedValue(response(snapshot()));
    vi.stubGlobal("fetch", fetch);
    poller.start();
    await settle();
    await vi.advanceTimersByTimeAsync(59999);
    expect(fetch).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(fetch).toHaveBeenCalledTimes(2);
    hidden.mockReturnValue(false);
    document.dispatchEvent(new Event("visibilitychange"));
    await settle();
    expect(fetch).toHaveBeenCalledTimes(3);
  });
  it("coalesces requests without overlapping an in-flight read", async () => {
    let resolve!: (value: Response) => void;
    const fetch = vi
      .fn()
      .mockReturnValueOnce(
        new Promise<Response>((done) => {
          resolve = done;
        }),
      )
      .mockResolvedValue(response(snapshot()));
    vi.stubGlobal("fetch", fetch);
    poller.start();
    void poller.poll();
    void poller.poll();
    expect(fetch).toHaveBeenCalledTimes(1);
    resolve(response(snapshot()));
    await settle();
    expect(fetch).toHaveBeenCalledTimes(2);
  });
  it("guards refresh, uses an optimistic 15 second busy window, and then polls", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response(snapshot()))
      .mockResolvedValueOnce(response({ accepted: true }, 202))
      .mockResolvedValue(response(snapshot()));
    vi.stubGlobal("fetch", fetch);
    poller.start();
    await settle();
    await poller.requestRefresh();
    await settle();
    expect(poller.refreshBusy()).toBe(true);
    expect(fetch.mock.calls[1][0]).toBe("/api/refresh");
    expect(fetch.mock.calls[1][1]).toMatchObject({
      method: "POST",
      headers: { "X-Dashboard-Refresh": "1" },
      cache: "no-store",
      credentials: "same-origin",
    });
    expect(fetch.mock.calls[1][1]).not.toHaveProperty("body");
    expect(fetch.mock.calls[2][0]).toBe("/api/snapshot");
    await poller.requestRefresh();
    expect(poller.getState().notice).toBe("A refresh is already running");
    expect(fetch).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(15000);
    expect(poller.refreshBusy()).toBe(false);
  });
  it("clears optimistic busy when the server starts a requested refresh", async () => {
    const started = snapshot({
      refresh: {
        ...snapshot().refresh!,
        inProgress: true,
        startedAt: "2026-10-01T12:00:01Z",
      },
    });
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response(snapshot()))
      .mockResolvedValueOnce(response({ accepted: true }, 202))
      .mockResolvedValue(response(started));
    vi.stubGlobal("fetch", fetch);
    poller.start();
    await settle();
    await poller.requestRefresh();
    await settle();
    expect(poller.getState().expectRefreshUntil).toBe(0);
    expect(poller.refreshBusy()).toBe(true);
  });
  it.each([false, "network"])(
    "reports rejected or failed refresh: %s",
    async (result) => {
      const fetch = vi.fn().mockResolvedValueOnce(response(snapshot()));
      if (result === "network")
        fetch.mockRejectedValueOnce(new Error("offline"));
      else fetch.mockResolvedValueOnce(response({ accepted: false }, 202));
      fetch.mockResolvedValue(response(snapshot()));
      vi.stubGlobal("fetch", fetch);
      poller.start();
      await settle();
      await poller.requestRefresh();
      await settle();
      expect(poller.refreshBusy()).toBe(false);
      expect(poller.getState().notice).toBe(
        result === "network"
          ? "Cannot reach the dashboard service"
          : "The platform was checked moments ago",
      );
      expect(fetch).toHaveBeenCalledTimes(3);
    },
  );
  it("cleans timers and aborts reads; stale completions cannot replace a new mount", async () => {
    let resolve!: (value: Response) => void;
    const fetch = vi
      .fn()
      .mockReturnValueOnce(
        new Promise<Response>((done) => {
          resolve = done;
        }),
      )
      .mockResolvedValue(response(snapshot()));
    vi.stubGlobal("fetch", fetch);
    poller.start();
    const signal = fetch.mock.calls[0][1].signal;
    poller.stop();
    expect(signal.aborted).toBe(true);
    poller.start();
    await settle();
    resolve(
      response({
        schemaVersion: 1,
        state: "pending",
        platform: snapshot().platform,
      }),
    );
    await settle();
    expect(poller.getState().snapshot?.state).toBe("ready");
    poller.stop();
    await vi.advanceTimersByTimeAsync(60000);
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});
