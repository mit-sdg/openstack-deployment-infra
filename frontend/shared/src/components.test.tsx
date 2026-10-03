import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import {
  AccountMenu,
  Alert,
  Badge,
  Button,
  Checkbox,
  CodeBlock,
  CopyField,
  CopyId,
  DataTable,
  Dialog,
  ErrorAlert,
  LoadError,
  Field,
  Input,
  PasswordInput,
  RelativeTime,
  StatusText,
  SegmentedControl,
  Select,
  ThemeToggle,
  ToastProvider,
  useToast,
} from "./index";

describe("form fields", () => {
  it("labels the control and describes it with its hint and error", () => {
    render(
      <Field
        label="Health path"
        hint="Use /health."
        error="Start with a slash."
      >
        <Input defaultValue="health" />
      </Field>,
    );
    const input = screen.getByLabelText("Health path");
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAccessibleDescription(
      "Start with a slash. Use /health.",
    );
  });
  it("keeps optional markers out of the accessible label", () => {
    render(
      <Field label="Build script" optional>
        <Select defaultValue="a">
          <option value="a">A</option>
        </Select>
      </Field>,
    );
    expect(screen.getByLabelText("Build script")).toBeInstanceOf(
      HTMLSelectElement,
    );
    expect(screen.getByText("Optional")).toBeVisible();
  });
  it("toggles password visibility without changing the value", () => {
    render(
      <Field label="Password">
        <PasswordInput defaultValue="secret" />
      </Field>,
    );
    const input = screen.getByLabelText("Password");
    expect(input).toHaveAttribute("type", "password");
    fireEvent.click(screen.getByRole("button", { name: "Show password" }));
    expect(input).toHaveAttribute("type", "text");
    expect(
      screen.getByRole("button", { name: "Hide password" }),
    ).toHaveAttribute("aria-pressed", "true");
  });
  it("names checkboxes by their label and description", () => {
    render(<Checkbox label="Confirm" description="This restarts the app." />);
    expect(
      screen.getByRole("checkbox", { name: "Confirm" }),
    ).toHaveAccessibleDescription("This restarts the app.");
  });
  it("uses native radios for segmented choices", () => {
    function Choice() {
      const [value, setValue] = useState<"a" | "b">("a");
      return (
        <SegmentedControl
          label="Method"
          name="method"
          value={value}
          onChange={setValue}
          options={[
            { value: "a", label: "First" },
            { value: "b", label: "Second" },
          ]}
        />
      );
    }
    render(<Choice />);
    expect(screen.getByRole("group", { name: "Method" })).toBeVisible();
    fireEvent.click(screen.getByRole("radio", { name: "Second" }));
    expect(screen.getByRole("radio", { name: "Second" })).toBeChecked();
  });
});

describe("buttons and feedback", () => {
  it("disables a loading button and marks it busy", () => {
    render(<Button loading>Save</Button>);
    const button = screen.getByRole("button", { name: "Save" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toHaveAttribute("type", "button");
  });
  it("announces danger alerts and focuses errors", () => {
    render(
      <>
        <Alert tone="warning">Heads up</Alert>
        <ErrorAlert error={new Error("Could not save.")} />
      </>,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Heads up");
    expect(screen.getByRole("alert")).toHaveTextContent("Could not save.");
    expect(screen.getByRole("alert")).toHaveFocus();
  });
  it("renders hostile badge text as text", () => {
    const { container } = render(
      <Badge tone="danger">{"<img src=x onerror=1>"}</Badge>,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("[data-tone]")).toHaveAttribute(
      "data-tone",
      "danger",
    );
  });
  it("shows and dismisses toasts in a polite live region", () => {
    vi.useFakeTimers();
    function Trigger() {
      const toast = useToast();
      return <button onClick={() => toast("Saved")}>Go</button>;
    }
    render(
      <ToastProvider>
        <Trigger />
      </ToastProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Go" }));
    const region = screen.getByText("Saved").closest("[aria-live]");
    expect(region).toHaveAttribute("aria-live", "polite");
    act(() => vi.advanceTimersByTime(5000));
    expect(screen.queryByText("Saved")).toBeNull();
    vi.useRealTimers();
  });
});

describe("data display", () => {
  it("keeps table semantics and labels cells for stacked rows", () => {
    render(
      <DataTable
        label="Apps"
        rows={[{ id: "1", name: "demo", status: "Healthy" }]}
        rowKey={(row) => row.id}
        columns={[
          {
            key: "name",
            header: "Name",
            mobile: "title",
            cell: (row) => row.name,
          },
          { key: "status", header: "Status", cell: (row) => row.status },
        ]}
      />,
    );
    const table = screen.getByRole("table", { name: "Apps" });
    const [, row] = within(table).getAllByRole("row");
    expect(within(row).getAllByRole("cell")[1]).toHaveAttribute(
      "data-label",
      "Status",
    );
    expect(screen.getByRole("columnheader", { name: "Name" })).toBeVisible();
  });
  it("makes rows clickable without hijacking links inside them", () => {
    const open = vi.fn();
    render(
      <DataTable
        label="Apps"
        rows={[{ id: "1", name: "demo" }]}
        rowKey={(row) => row.id}
        onRowClick={(row) => open(row.id)}
        columns={[
          {
            key: "name",
            header: "Name",
            cell: (row) => <a href="#demo">{row.name}</a>,
          },
          { key: "note", header: "Note", cell: () => "Plain text" },
        ]}
      />,
    );
    fireEvent.click(screen.getByRole("link", { name: "demo" }));
    expect(open).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("Plain text"));
    expect(open).toHaveBeenCalledWith("1");
  });
  it("shows the empty state instead of an empty table", () => {
    render(
      <DataTable
        label="Apps"
        rows={[]}
        rowKey={() => ""}
        columns={[]}
        empty={<p>None yet</p>}
      />,
    );
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getByText("None yet")).toBeVisible();
  });
  it("renders logs as focusable text", () => {
    render(<CodeBlock label="Build log">{"<b>not html</b>"}</CodeBlock>);
    const log = screen.getByLabelText("Build log");
    expect(log).toHaveAttribute("tabindex", "0");
    expect(log.querySelector("b")).toBeNull();
  });
  it("copies a read-only value", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(
      <CopyField label="Setup link" value="https://example.com/setup#token" />,
    );
    expect(screen.getByLabelText("Setup link")).toHaveAttribute("readonly");
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Copy" })),
    );
    expect(writeText).toHaveBeenCalledWith("https://example.com/setup#token");
    expect(screen.getByRole("button", { name: "Copied" })).toBeVisible();
  });
});

describe("overlays and shell", () => {
  it("opens a labelled dialog and closes it from the close button", () => {
    const showModal = vi.fn(function (this: HTMLDialogElement) {
      this.setAttribute("open", "");
    });
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: showModal,
    });
    const onClose = vi.fn();
    render(
      <Dialog open onClose={onClose} title="Deploy app">
        Body
      </Dialog>,
    );
    expect(showModal).toHaveBeenCalled();
    expect(
      screen.getByRole("dialog", { name: "Deploy app" }),
    ).toHaveTextContent("Body");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalled();
  });
  it("opens the account menu and closes it with Escape", () => {
    render(
      <AccountMenu name="Alice Student" detail="alice">
        <button>Sign out</button>
      </AccountMenu>,
    );
    const trigger = screen.getByRole("button", {
      name: "Account: Alice Student",
    });
    expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();
    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: "Sign out" })).toBeVisible();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(trigger).toHaveFocus();
  });
  it("cycles the theme preference under the app's key only", () => {
    localStorage.setItem("another-app", "dark");
    render(<ThemeToggle storageKey="this-app" />);
    const button = screen.getByRole("button", { name: /Theme: System/ });
    fireEvent.click(button);
    expect(localStorage.getItem("this-app")).toBe("light");
    fireEvent.click(button);
    expect(document.documentElement.dataset.theme).toBe("dark");
    fireEvent.click(button);
    expect(localStorage.getItem("this-app")).toBeNull();
    expect(document.documentElement.dataset.theme).toBeUndefined();
    expect(localStorage.getItem("another-app")).toBe("dark");
  });
});

describe("compact values", () => {
  it("copies the whole ID while showing its start", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const id = "22222222-2222-4222-8222-222222222222";
    render(<CopyId value={id} label="owner ID" />);
    expect(screen.getByText("22222222")).toHaveAttribute("title", id);
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Copy owner ID" })),
    );
    expect(writeText).toHaveBeenCalledWith(id);
    expect(screen.getByRole("status")).toHaveTextContent("Copied to clipboard");
  });
  it("shows relative time with the exact time available", () => {
    const value = new Date(Date.now() - 21 * 60000).toISOString();
    const { container } = render(
      <>
        <RelativeTime value={value} />
        <RelativeTime value={null} empty="Never" />
      </>,
    );
    const time = container.querySelector("time")!;
    expect(time).toHaveTextContent("21 minutes ago");
    expect(time).toHaveAttribute("dateTime", value);
    expect(time).toHaveAttribute("title", new Date(value).toLocaleString());
    expect(screen.getByText("Never")).toHaveClass("ui-text-subtle");
  });
  it("renders quiet status as text with a decorative dot", () => {
    const { container } = render(<StatusText>Healthy</StatusText>);
    expect(container.querySelector(".ui-badge")).toBeNull();
    expect(screen.getByText("Healthy")).toBeVisible();
    expect(container.querySelector("[aria-hidden='true']")).not.toBeNull();
  });
  it("marks secondary and meta columns for the compact phone layout", () => {
    render(
      <DataTable
        label="Owners"
        rows={[{ id: "1", name: "Alice", username: "alice" }]}
        rowKey={(row) => row.id}
        columns={[
          {
            key: "name",
            header: "Name",
            mobile: "title",
            cell: (row) => row.name,
          },
          {
            key: "username",
            header: "Username",
            mobile: "secondary",
            cell: (row) => row.username,
          },
          {
            key: "seen",
            header: "Last seen",
            mobile: "meta",
            cell: () => "today",
          },
        ]}
      />,
    );
    expect(screen.getByText("alice").closest("td")).toHaveAttribute(
      "data-mobile",
      "secondary",
    );
    expect(screen.getByText("today").closest("td")).toHaveAttribute(
      "data-mobile",
      "meta",
    );
  });
});

describe("round 3 pieces", () => {
  it("describes a segmented control with its hint", () => {
    render(
      <SegmentedControl
        label="Method"
        name="m"
        value="a"
        onChange={() => {}}
        options={[{ value: "a", label: "A" }]}
        hint="Pick one."
      />,
    );
    expect(
      screen.getByRole("group", { name: "Method" }),
    ).toHaveAccessibleDescription("Pick one.");
  });
  it("announces load errors with Retry without stealing focus", () => {
    const retry = vi.fn();
    render(<LoadError onRetry={retry}>Couldn't load your apps.</LoadError>);
    const alert = screen.getByRole("alert");
    expect(alert).not.toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(retry).toHaveBeenCalledOnce();
  });
});
