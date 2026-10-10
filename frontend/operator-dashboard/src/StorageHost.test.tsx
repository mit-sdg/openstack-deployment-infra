import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { StorageHost } from "./StorageHost";
import { DrawerContent } from "./Drawer";
import { application, snapshot } from "./test-fixtures";
import { Applications } from "./Applications";

it("renders cached host capacity, stale samples and a blocked resource in the drawer", () => {
  const host = {
    measuredAt: "2026-10-10T12:00:00Z",
    stale: true,
    cpuCount: 4,
    loadAverage: [4, 3, 2],
    memory: { totalBytes: 8589934592, availableBytes: 858993459 },
    dataVolume: { totalBytes: 536870912000, usedBytes: 515396075520 },
    containers: [
      {
        name: "example-postgres",
        usedBytes: 1073741824,
        limitBytes: 3221225472,
      },
      { name: "example-mongo", usedBytes: 1073741824, limitBytes: 3221225472 },
      { name: "example-garage", usedBytes: 1073741824, limitBytes: 3221225472 },
      {
        name: "example-registry",
        usedBytes: 1073741824,
        limitBytes: 3221225472,
      },
      { name: "other-worker", usedBytes: 1073741824, limitBytes: 3221225472 },
    ],
    postgresConnections: { current: 42, limit: 400 },
    mongoConnections: { current: 32, limit: 1000 },
  };
  const blocked = application({
    storage: [
      {
        id: "resource",
        type: "mongo",
        typeLabel: "MongoDB",
        name: "default",
        label: null,
        status: { key: "active", label: "Active", tone: "good" },
        quotas: {
          sizeBytes: 2147483648,
          connections: 10,
          memoryBytes: 536870912,
          cpuMillicores: 500,
        },
        lastVerifiedAt: null,
        usage: {
          usedBytes: 3221225472,
          objectCount: null,
          currentConnections: null,
          instanceMemoryBytes: null,
          cpuTimeMilliseconds: null,
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
  });
  render(
    <>
      <StorageHost host={host} />
      <Applications
        snapshot={snapshot({ applications: [blocked] })}
        filter="attention"
        setFilter={() => {}}
        openApp={() => {}}
      />
      <DrawerContent
        app={blocked}

        notify={() => {}}
      />
    </>,
  );
  expect(screen.getByRole("heading", { name: "Storage host" })).toBeVisible();
  expect(
    screen.getByRole("button", { name: /sample-app, Serving/ }),
  ).toBeVisible();
  expect(screen.getByRole("progressbar", { name: "Memory" })).toHaveAttribute(
    "aria-valuetext",
    "7.2 of 8 GiB",
  );
  expect(
    screen
      .getByRole("progressbar", { name: "Memory" })
      .closest(".storage-host__metric"),
  ).toHaveAttribute("data-tone", "warning");
  expect(
    screen
      .getByRole("progressbar", { name: "Data volume" })
      .closest(".storage-host__metric"),
  ).toHaveAttribute("data-tone", "critical");
  expect(
    screen
      .getByRole("progressbar", { name: "Load (1 minute)" })
      .closest(".storage-host__metric"),
  ).toHaveAttribute("data-tone", "warning");
  for (const product of [
    "PostgreSQL",
    "MongoDB",
    "Garage",
    "Registry",
    "other-worker",
  ])
    expect(
      screen.getByRole("progressbar", { name: `${product} memory` }),
    ).toHaveAttribute("aria-valuetext", "1 of 3 GiB");
  expect(screen.getByText("42 of 400")).toBeVisible();
  expect(screen.getByText("32 of 1,000")).toBeVisible();
  expect(screen.getByText("Stale", { exact: true })).toBeVisible();
  expect(screen.getByText(/Writes paused/)).toBeVisible();
});
