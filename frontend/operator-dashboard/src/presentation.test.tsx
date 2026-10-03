import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Applications, compareApps } from "./Applications";
import { DrawerContent } from "./Drawer";
import { Time, relative, safeHref } from "./presentation";
import { groupIssues } from "./Sections";
import { application, snapshot } from "./test-fixtures";

describe("operator presentation parity", () => {
  it("renders hostile strings as text and rejects executable URLs", () => {
    const hostile = "<img src=x onerror=alert(1)>";
    const app = application({
      slug: hostile,
      url: "javascript:alert(1)",
      recoveryNote: hostile,
      status: { key: "needs-recovery", label: hostile, tone: "warning" },
    });
    const { container } = render(
      <>
        <Applications
          snapshot={snapshot({ applications: [app] })}
          filter="all"
          setFilter={() => {}}
          openApp={() => {}}
        />
        <DrawerContent app={app} notify={() => {}} />
      </>,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
    expect(container.textContent).toContain(hostile);
    expect(safeHref("http://sample.example.invalid")).toBeNull();
    expect(safeHref("https://sample.example.invalid")).not.toBeNull();
  });
  it("preserves typed search and keyboard focus across snapshot updates", () => {
    const props = {
      filter: "all" as const,
      setFilter: vi.fn(),
      openApp: vi.fn(),
    };
    const { rerender } = render(
      <Applications {...props} snapshot={snapshot()} />,
    );
    const search = screen.getByRole("searchbox");
    search.focus();
    fireEvent.change(search, { target: { value: "sample" } });
    rerender(
      <Applications
        {...props}
        snapshot={snapshot({ generatedAt: "2026-10-01T12:01:00Z" })}
      />,
    );
    expect(search).toHaveFocus();
    expect(search).toHaveValue("sample");
    expect(
      screen.getByRole("button", { name: /sample-app, Serving/ }),
    ).toBeVisible();
  });
  it("distinguishes empty inventories from empty search matches", () => {
    const { rerender } = render(
      <Applications
        snapshot={snapshot({ applications: [] })}
        filter="all"
        setFilter={() => {}}
        openApp={() => {}}
      />,
    );
    expect(screen.getByText("No applications yet")).toBeVisible();
    rerender(
      <Applications
        snapshot={snapshot()}
        filter="all"
        setFilter={() => {}}
        openApp={() => {}}
      />,
    );
    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "absent" },
    });
    expect(screen.getByText("No applications match")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getByRole("searchbox")).toHaveFocus();
  });
  it("keeps status precedence and slowest-first latency order", () => {
    const good = application(),
      bad = application({
        id: "bad",
        status: { key: "failing", label: "Failing", tone: "critical" },
        route: { ...good.route!, latencyMs: 1000 },
      });
    expect(compareApps(bad, good, "status")).toBeLessThan(0);
    expect(compareApps(bad, good, "latency")).toBeLessThan(0);
  });
  it("groups three identical application causes and preserves total issues separately", () => {
    const issues = Array.from({ length: 3 }, (_, index) => ({
      tone: "critical" as const,
      scope: "application",
      target: String(index),
      subject: `app-${index}`,
      summary: "Failing",
      detail: "same cause",
    }));
    expect(groupIssues(issues)).toHaveLength(1);
    expect(groupIssues(issues)[0].subject).toBe("3 applications");
    expect(groupIssues(issues.slice(0, 2))).toHaveLength(2);
  });
  it("uses exact age thresholds, future clamp and absolute time metadata", () => {
    const now = Date.parse("2026-10-01T12:00:00Z");
    expect(relative(new Date(now - 4000).toISOString(), now)).toBe("just now");
    expect(relative(new Date(now - 5000).toISOString(), now)).toBe("5 s ago");
    expect(relative(new Date(now - 60000).toISOString(), now)).toBe(
      "1 min ago",
    );
    expect(relative(new Date(now - 3600000).toISOString(), now)).toBe(
      "1 h ago",
    );
    expect(relative(new Date(now - 172800000).toISOString(), now)).toBe(
      "2 d ago",
    );
    expect(relative(new Date(now + 5000).toISOString(), now)).toBe("just now");
    expect(relative("invalid", now)).toBe("never");
    const { container } = render(
      <Time value="2026-10-01T11:59:00Z" now={now} />,
    );
    expect(container.querySelector("time")).toHaveAttribute(
      "datetime",
      "2026-10-01T11:59:00Z",
    );
    expect(container.querySelector("time")).toHaveAttribute("title");
  });
});
