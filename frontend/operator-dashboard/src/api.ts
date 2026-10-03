import type { Snapshot } from "./snapshot";

export const SNAPSHOT_URL = "/api/snapshot";
export const REFRESH_URL = "/api/refresh";
export async function readSnapshot(etag: string | null, signal: AbortSignal) {
  const response = await fetch(SNAPSHOT_URL, {
    headers: etag ? { "If-None-Match": etag } : {},
    cache: "no-store",
    credentials: "same-origin",
    signal,
  });
  if (response.status === 304) return { snapshot: null, etag };
  if (!response.ok)
    throw new Error(`snapshot request failed with HTTP ${response.status}`);
  const snapshot = (await response.json()) as Snapshot;
  if (
    snapshot.schemaVersion !== 1 ||
    !["pending", "ready"].includes(snapshot.state)
  )
    throw new Error("Unsupported dashboard snapshot");
  return { snapshot, etag: response.headers.get("ETag") };
}
export async function refreshSnapshot(signal: AbortSignal) {
  const response = await fetch(REFRESH_URL, {
    method: "POST",
    headers: { "X-Dashboard-Refresh": "1" },
    cache: "no-store",
    credentials: "same-origin",
    signal,
  });
  return response.ok ? Boolean((await response.json()).accepted) : false;
}
