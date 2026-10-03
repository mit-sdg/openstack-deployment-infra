import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { BoundaryText, StatusBadge, ThemeButton } from "./index";

describe("shared presentation", () => {
  it("keeps hostile content as text and uses explicit server labels/tones", () => {
    const hostile = "<script>alert(1)</script>";
    const { container } = render(
      <>
        <StatusBadge label={hostile} tone="warning" />
        <BoundaryText text={hostile} />
      </>,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toBe(hostile + hostile);
    expect(container.querySelector(".status")).toHaveAttribute(
      "data-tone",
      "warning",
    );
  });
  it("cycles only the selected app preference and clears system selection", () => {
    localStorage.setItem("another-app", "dark");
    render(<ThemeButton storageKey="this-app" />);
    const button = screen.getByRole("button");
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
