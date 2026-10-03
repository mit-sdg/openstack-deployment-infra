import { useState } from "react";
import { Icon } from "../Icon";

export type ThemePreference = "system" | "light" | "dark";

/**
 * Cycles system → light → dark, stores only the explicit choice under this
 * app's key and mirrors it to <html data-theme>, which theme.js restores
 * before first paint.
 */
export function useThemePreference(storageKey: string) {
  const [theme, setTheme] = useState<ThemePreference>(() => {
    const saved = document.documentElement.dataset.theme;
    return saved === "light" || saved === "dark" ? saved : "system";
  });
  function cycle() {
    const next: ThemePreference =
      theme === "system" ? "light" : theme === "light" ? "dark" : "system";
    setTheme(next);
    if (next === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = next;
    try {
      if (next === "system") localStorage.removeItem(storageKey);
      else localStorage.setItem(storageKey, next);
    } catch {
      /* The OS preference remains available. */
    }
  }
  return { theme, cycle };
}

const names = { system: "System", light: "Light", dark: "Dark" } as const;

/** Icon button for the shell header. */
export function ThemeToggle({ storageKey }: { storageKey: string }) {
  const { theme, cycle } = useThemePreference(storageKey);
  const label = `Theme: ${names[theme]}. Change theme`;
  return (
    <button
      type="button"
      className="ui-icon-button"
      aria-label={label}
      title={label}
      onClick={cycle}
    >
      <Icon
        name={theme === "dark" ? "moon" : theme === "light" ? "sun" : "monitor"}
      />
    </button>
  );
}
