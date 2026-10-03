import { useEffect, useState, useSyncExternalStore } from "react";
import { SnapshotPoller } from "./polling";

export function useSnapshot() {
  const [poller] = useState(() => new SnapshotPoller());
  const state = useSyncExternalStore(poller.subscribe, poller.getState);
  useEffect(() => {
    poller.start();
    return () => poller.stop();
  }, [poller]);
  return { ...state, poller };
}
export function useNow() {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  return now;
}
