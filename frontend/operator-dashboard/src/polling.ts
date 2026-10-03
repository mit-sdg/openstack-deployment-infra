import { readSnapshot, refreshSnapshot } from "./api";
import type { Snapshot } from "./snapshot";

export type PollState = {
  snapshot: Snapshot | null;
  connected: boolean;
  expectRefreshUntil: number;
  notice: string;
  noticeSequence: number;
};
export class SnapshotPoller {
  private state: PollState = {
    snapshot: null,
    connected: true,
    expectRefreshUntil: 0,
    notice: "",
    noticeSequence: 0,
  };
  private listeners = new Set<() => void>();
  private etag: string | null = null;
  private baseline: string | null = null;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private active = false;
  private inFlight = false;
  private pollAgain = false;
  private generation = 0;
  private controller = new AbortController();

  getState = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private update(change: Partial<PollState>) {
    this.state = { ...this.state, ...change };
    for (const listener of this.listeners) listener();
  }
  notice(message: string) {
    this.update({
      notice: message,
      noticeSequence: this.state.noticeSequence + 1,
    });
  }
  refreshBusy(now = Date.now()) {
    return (
      Boolean(this.state.snapshot?.refresh?.inProgress) ||
      this.state.snapshot?.state === "pending" ||
      now < this.state.expectRefreshUntil
    );
  }
  nextDelay() {
    if (document.hidden) return 60_000;
    return !this.state.snapshot || this.refreshBusy() || !this.state.connected
      ? 2_000
      : 10_000;
  }
  private visibility = () => {
    if (!document.hidden) void this.poll();
  };
  start() {
    if (this.active) return;
    this.active = true;
    this.controller = new AbortController();
    document.addEventListener("visibilitychange", this.visibility);
    void this.poll();
  }
  stop() {
    this.active = false;
    this.generation++;
    this.controller.abort();
    clearTimeout(this.timer);
    this.inFlight = false;
    this.pollAgain = false;
    document.removeEventListener("visibilitychange", this.visibility);
  }
  async poll() {
    if (!this.active) return;
    if (this.inFlight) {
      this.pollAgain = true;
      return;
    }
    this.inFlight = true;
    clearTimeout(this.timer);
    const generation = this.generation;
    try {
      const result = await readSnapshot(this.etag, this.controller.signal);
      if (!this.active || generation !== this.generation) return;
      const snapshot = result.snapshot;
      if (snapshot) {
        this.etag = result.etag;
        const started =
          snapshot.refresh?.inProgress ||
          snapshot.refresh?.startedAt !== this.baseline;
        this.update({
          snapshot,
          connected: true,
          expectRefreshUntil: started ? 0 : this.state.expectRefreshUntil,
        });
      } else this.update({ connected: true });
    } catch {
      if (!this.active || generation !== this.generation) return;
      this.update({ connected: false });
    } finally {
      if (generation === this.generation) this.inFlight = false;
    }
    if (!this.active || generation !== this.generation) return;
    if (this.pollAgain) {
      this.pollAgain = false;
      void this.poll();
    } else
      this.timer = setTimeout(() => {
        void this.poll();
      }, this.nextDelay());
  }
  async requestRefresh() {
    if (!this.active) return;
    if (this.refreshBusy()) {
      this.notice("A refresh is already running");
      return;
    }
    this.baseline = this.state.snapshot?.refresh?.startedAt ?? null;
    this.update({ expectRefreshUntil: Date.now() + 15_000 });
    const generation = this.generation;
    try {
      const accepted = await refreshSnapshot(this.controller.signal);
      if (!this.active || generation !== this.generation) return;
      if (!accepted) {
        this.update({ expectRefreshUntil: 0 });
        this.notice("The platform was checked moments ago");
      }
    } catch {
      if (!this.active || generation !== this.generation) return;
      this.update({ expectRefreshUntil: 0 });
      this.notice("Cannot reach the dashboard service");
    }
    void this.poll();
  }
}
