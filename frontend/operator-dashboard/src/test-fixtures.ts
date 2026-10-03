import { describe, expect, it } from "vitest";
import type { Application, ReadySnapshot } from "./snapshot";

export function application(overrides: Partial<Application> = {}): Application {
  return {
    id: "app-one",
    slug: "sample-app",
    url: "https://sample-app.example.invalid",
    enabled: true,
    status: { key: "serving", label: "Serving", tone: "good" },
    recoveryNote: null,
    route: {
      outcome: "serving",
      tone: "good",
      summary: "HTTP 200",
      detail: "Accepted deployment answers",
      url: "https://sample-app.example.invalid/health",
      httpStatus: 200,
      latencyMs: 100,
      checkedAt: "2026-10-01T12:00:00Z",
    },
    checks: [],
    deployment: null,
    attempts: [],
    operation: null,
    sizing: { flavor: null, cpuMHz: null, memoryMiB: null },
    storage: [],
    createdAt: null,
    updatedAt: null,
    deletedAt: null,
    ...overrides,
  };
}
export function snapshot(
  overrides: Partial<ReadySnapshot> = {},
): ReadySnapshot {
  return {
    schemaVersion: 1,
    state: "ready",
    platform: {
      name: "Fixture platform",
      domain: "example.invalid",
      region: "Fixture region",
      namespace: "fixture",
      release: null,
    },
    generatedAt: "2026-10-01T12:00:00Z",
    summary: {
      tone: "good",
      headline: "Platform healthy",
      detail: "No issues detected",
      counts: {
        roles: { total: 0, healthy: 0 },
        applications: {
          total: 1,
          serving: 1,
          attention: 0,
          changing: 0,
          stopped: 0,
        },
        operations: { running: 0, recovery: 0 },
        backup: {
          ageHours: null,
          tone: "neutral",
          state: "not-run",
          offsite: "not-run",
        },
      },
    },
    applications: [application()],
    roles: [],
    operations: [],
    operationsTruncated: false,
    issues: [],
    sources: [],
    checks: { available: false, stale: false, checkedAt: null, items: [] },
    refresh: {
      intervalSeconds: 60,
      inProgress: false,
      startedAt: null,
      completedAt: null,
      durationMs: null,
      error: null,
    },
    ...overrides,
  };
}
