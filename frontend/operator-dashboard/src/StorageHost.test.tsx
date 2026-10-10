import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { StorageHost } from "./StorageHost";
import { DrawerContent } from "./Drawer";
import { application } from "./test-fixtures";

it("renders cached host capacity, stale samples and a blocked resource in the drawer", () => {
  const host = {
    measuredAt: "2026-10-10T12:00:00Z",
    stale: true,
    cpuCount: 4,
    loadAverage: [0.2, 0.3, 0.4],
    memory: { totalBytes: 8589934592, availableBytes: 2147483648 },
    dataVolume: { totalBytes: 536870912000, usedBytes: 107374182400 },
    containers: [
      { name: "postgres", usedBytes: 1073741824, limitBytes: 3221225472 },
    ],
    postgresConnections: { current: 42, limit: 400 },
    mongoConnections: { current: 32, limit: 1000 },
  };
  render(
    <>
      <StorageHost host={host} />
      <DrawerContent
        app={application({
          storage: [
            {
              id: "resource",
              type: "mongo",
              typeLabel: "MongoDB",
              name: "default",
              label: null,
              status: { key: "active", label: "Active", tone: "good" },
              quotas: { measuredTargetBytes: 2147483648 },
              lastVerifiedAt: null,
              usage: {
                usedBytes: 3221225472,
                objectCount: null,
                currentConnections: null,
                measuredAt: host.measuredAt,
                stale: true,
              },
              writeBlock: {
                blocked: true,
                reason: "size_limit_exceeded",
                since: host.measuredAt,
              },
            },
          ],
        })}
        notify={() => {}}
      />
    </>,
  );
  expect(screen.getByRole("heading", { name: "Storage host" })).toBeVisible();
  expect(screen.getByText("6 GiB of 8 GiB")).toBeVisible();
  expect(screen.getByText("100 GiB of 500 GiB")).toBeVisible();
  expect(screen.getByText("1 GiB of 3 GiB")).toBeVisible();
  expect(screen.getByText("42 of 400")).toBeVisible();
  expect(screen.getByText("32 of 1000")).toBeVisible();
  expect(screen.getByText(/Stale observation/)).toBeVisible();
  expect(screen.getByText(/Writes paused/)).toBeVisible();
});
