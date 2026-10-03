import type { Tone } from "@openstack-platform/ui";

export type Status = { key: string; label: string; tone: Tone };
export type History = { at: string; tone: Tone };
export type Operation = {
  id: string;
  kind: string;
  label: string;
  plane: string;
  applicationId: string | null;
  subject: string;
  status: Status;
  phase: string | null;
  startedAt: string | null;
  updatedAt: string | null;
  deadlineAt: string | null;
  error: string | null;
  cleanupState: string | null;
};
export type Deployment = {
  id: string;
  status: Status;
  commit: string | null;
  commitUrl: string | null;
  ref: string | null;
  repository: string | null;
  runtime: string | null;
  port: number | null;
  healthPath: string | null;
  configurationRevision: number | null;
  imageDigest: string | null;
  error: string | null;
  cleanupState: string | null;
  requestedAt: string | null;
  updatedAt: string | null;
  acceptedAt: string | null;
  lastHealthyAt: string | null;
};
export type Storage = {
  id: string;
  type: string;
  typeLabel: string;
  name: string;
  label: string | null;
  status: Status;
  quotas: Record<string, number>;
  lastVerifiedAt: string | null;
};
export type Application = {
  id: string;
  slug: string;
  url: string | null;
  enabled: boolean;
  status: Status;
  recoveryNote: string | null;
  route: {
    outcome: string;
    tone: Tone;
    summary: string;
    detail: string;
    url: string;
    httpStatus: number | null;
    latencyMs: number | null;
    checkedAt: string | null;
  } | null;
  checks: History[];
  deployment: Deployment | null;
  attempts: Deployment[];
  operation: Operation | null;
  sizing: {
    flavor: string | null;
    cpuMHz: number | null;
    memoryMiB: number | null;
  };
  storage: Storage[];
  createdAt: string | null;
  updatedAt: string | null;
  deletedAt: string | null;
};
export type Role = {
  role: string;
  name: string;
  lifetime: string;
  lifetimeLabel: string;
  summary: string;
  status: Status;
  signals: {
    label: string;
    value: string;
    tone: Tone;
    detail: string | null;
  }[];
  facts: { label: string; value: string }[];
  image: {
    name: string | null;
    commit: string | null;
    selectedAt: string | null;
  } | null;
};
export type Issue = {
  tone: Tone;
  scope: string;
  target: string;
  subject: string;
  summary: string;
  detail: string;
};
export type Source = {
  key: string;
  label: string;
  ok: boolean;
  observedAt: string | null;
  error: string | null;
};
export type Refresh = {
  intervalSeconds: number;
  inProgress: boolean;
  startedAt: string | null;
  completedAt: string | null;
  durationMs: number | null;
  error: string | null;
};
type Base = {
  schemaVersion: 1;
  platform: {
    name: string;
    domain: string;
    region: string;
    namespace: string;
    release: string | null;
  };
  refresh?: Refresh;
};
export type PendingSnapshot = Base & { state: "pending" };
export type ReadySnapshot = Base & {
  state: "ready";
  generatedAt: string;
  summary: {
    tone: Tone;
    headline: string;
    detail: string;
    counts: {
      roles: { total: number; healthy: number };
      applications: {
        total: number;
        serving: number;
        attention: number;
        changing: number;
        stopped: number;
      };
      operations: { running: number; recovery: number };
      backup: {
        ageHours: number | null;
        tone: Tone;
        state: string;
        offsite: string;
      };
    };
  };
  roles: Role[];
  applications: Application[];
  operations: Operation[];
  operationsTruncated: boolean;
  issues: Issue[];
  sources: Source[];
  checks: {
    available: boolean;
    stale: boolean;
    checkedAt: string | null;
    items: { label: string; detail: string; state: string; tone: Tone }[];
  };
};
export type Snapshot = PendingSnapshot | ReadySnapshot;
