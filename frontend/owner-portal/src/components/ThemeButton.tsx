import { useState } from 'react';

export function ThemeButton() {
  const [theme, setTheme] = useState(document.documentElement.dataset.theme ?? 'system');
  return (
    <button
      className="icon-button theme-button"
      aria-label={`Theme: ${theme}. Change theme`}
      onClick={() => {
        const next = theme === 'system' ? 'light' : theme === 'light' ? 'dark' : 'system';
        setTheme(next);
        if (next === 'system') delete document.documentElement.dataset.theme;
        else document.documentElement.dataset.theme = next;
        try {
          localStorage.setItem('owner-portal-theme', next);
        } catch {
          /* OS preference remains available. */
        }
      }}
    >
      <svg className="theme-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        {theme === 'dark' ? (
          <path d="M20 14.6A8.5 8.5 0 1 1 9.4 4a6.6 6.6 0 0 0 10.6 10.6Z" />
        ) : theme === 'light' ? (
          <>
            <circle cx="12" cy="12" r="4" />
            <path d="M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4" />
          </>
        ) : (
          <>
            <rect x="3" y="4" width="18" height="12.5" rx="2" />
            <path d="M8.5 20h7M12 16.5V20" />
          </>
        )}
      </svg>
      <span className="theme-label">{theme}</span>
    </button>
  );
}
