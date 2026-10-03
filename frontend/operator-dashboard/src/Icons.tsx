import type { Tone } from "@openstack-platform/ui";
export function Icon({
  name,
  className = "icon",
}: {
  name: string;
  className?: string;
}) {
  return (
    <svg className={className} aria-hidden="true" focusable="false">
      <use href={`#${name}`} />
    </svg>
  );
}
export function ToneIcon({ tone }: { tone: Tone }) {
  return (
    <svg
      className="sicon"
      data-tone={tone}
      aria-hidden="true"
      focusable="false"
    >
      <use href={`#s-${tone}`} />
    </svg>
  );
}
export function Sprite() {
  return (
    <svg className="sprite" aria-hidden="true" focusable="false">
      <symbol id="i-logo" viewBox="0 0 24 24">
        <path d="M12 2.8 21 7.6 12 12.4 3 7.6Z" />
        <path d="m3 12 9 4.8 9-4.8" />
        <path d="m3 16.4 9 4.8 9-4.8" />
      </symbol>
      <symbol id="i-lock" viewBox="0 0 24 24">
        <rect x="5" y="10.5" width="14" height="10" rx="2" />
        <path d="M8.5 10.5V7.5a3.5 3.5 0 0 1 7 0v3" />
      </symbol>
      <symbol id="i-refresh" viewBox="0 0 24 24">
        <path d="M20.5 12a8.5 8.5 0 1 1-2.5-6" />
        <path d="M20.5 3.5V8H16" />
      </symbol>
      <symbol id="i-theme-system" viewBox="0 0 24 24">
        <rect x="3" y="4" width="18" height="12.5" rx="2" />
        <path d="M8.5 20h7M12 16.5V20" />
      </symbol>
      <symbol id="i-theme-light" viewBox="0 0 24 24">
        <circle cx="12" cy="12" r="4" />
        <path d="M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4" />
      </symbol>
      <symbol id="i-theme-dark" viewBox="0 0 24 24">
        <path d="M20 14.6A8.5 8.5 0 1 1 9.4 4a6.6 6.6 0 0 0 10.6 10.6Z" />
      </symbol>
      <symbol id="i-search" viewBox="0 0 24 24">
        <circle cx="11" cy="11" r="6.5" />
        <path d="m20 20-4.2-4.2" />
      </symbol>
      <symbol id="i-external" viewBox="0 0 24 24">
        <path d="M14 4h6v6" />
        <path d="M20 4 11 13" />
        <path d="M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10" />
      </symbol>
      <symbol id="i-chevron" viewBox="0 0 24 24">
        <path d="m9.5 6 6 6-6 6" />
      </symbol>
      <symbol id="i-close" viewBox="0 0 24 24">
        <path d="M18 6 6 18M6 6l12 12" />
      </symbol>
      <symbol id="i-copy" viewBox="0 0 24 24">
        <rect x="9" y="9" width="11" height="11" rx="2" />
        <path d="M5.5 15H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1v.5" />
      </symbol>
      <symbol id="i-check" viewBox="0 0 24 24">
        <path d="m5 12.5 4.5 4.5L19 7.5" />
      </symbol>
      <symbol id="i-admin" viewBox="0 0 24 24">
        <rect x="5" y="5" width="14" height="14" rx="2.5" />
        <path d="M9.5 9.5h5v5h-5z" />
        <path d="M9.5 2.5v2.5M14.5 2.5v2.5M9.5 19v2.5M14.5 19v2.5M2.5 9.5H5M2.5 14.5H5M19 9.5h2.5M19 14.5h2.5" />
      </symbol>
      <symbol id="i-ingress" viewBox="0 0 24 24">
        <circle cx="12" cy="12" r="9" />
        <path d="M3 12h18" />
        <path d="M12 3c2.5 2.6 3.7 5.6 3.7 9s-1.2 6.4-3.7 9c-2.5-2.6-3.7-5.6-3.7-9S9.5 5.6 12 3Z" />
      </symbol>
      <symbol id="i-storage" viewBox="0 0 24 24">
        <ellipse cx="12" cy="5.5" rx="7.5" ry="2.75" />
        <path d="M4.5 5.5v13c0 1.5 3.4 2.75 7.5 2.75s7.5-1.25 7.5-2.75v-13" />
        <path d="M4.5 12c0 1.5 3.4 2.75 7.5 2.75s7.5-1.25 7.5-2.75" />
      </symbol>
      <symbol id="i-worker" viewBox="0 0 24 24">
        <rect x="3.5" y="4" width="17" height="7" rx="1.75" />
        <rect x="3.5" y="13" width="17" height="7" rx="1.75" />
        <path d="M7.5 7.5h.01M7.5 16.5h.01M11 7.5h2M11 16.5h2" />
      </symbol>
      <symbol id="i-builder" viewBox="0 0 24 24">
        <path d="m12 2.75 8 4.5v9.5l-8 4.5-8-4.5v-9.5Z" />
        <path d="m4 7.25 8 4.5 8-4.5" />
        <path d="M12 11.75v9.5" />
      </symbol>
      <symbol id="i-roles" viewBox="0 0 24 24">
        <path d="M12 2.8 21 7.6 12 12.4 3 7.6Z" />
        <path d="m3 12 9 4.8 9-4.8" />
        <path d="m3 16.4 9 4.8 9-4.8" />
      </symbol>
      <symbol id="i-apps" viewBox="0 0 24 24">
        <rect x="4" y="4" width="6.5" height="6.5" rx="1.5" />
        <rect x="13.5" y="4" width="6.5" height="6.5" rx="1.5" />
        <rect x="4" y="13.5" width="6.5" height="6.5" rx="1.5" />
        <rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.5" />
      </symbol>
      <symbol id="i-activity" viewBox="0 0 24 24">
        <path d="M3 12h4l2.5-7 5 14 2.5-7h4" />
      </symbol>
      <symbol id="i-backup" viewBox="0 0 24 24">
        <rect x="3" y="4" width="18" height="4.5" rx="1.25" />
        <path d="M5 8.5v10A1.5 1.5 0 0 0 6.5 20h11a1.5 1.5 0 0 0 1.5-1.5v-10" />
        <path d="M10 12.5h4" />
      </symbol>
      <symbol id="i-shield" viewBox="0 0 24 24">
        <path d="M12 3 5 6v5.5c0 4.4 2.9 7.9 7 9.5 4.1-1.6 7-5.1 7-9.5V6Z" />
        <path d="m9 12 2.2 2.2L15.5 10" />
      </symbol>
      <symbol id="i-commit" viewBox="0 0 24 24">
        <circle cx="12" cy="12" r="3.25" />
        <path d="M3 12h5.75M15.25 12H21" />
      </symbol>
      <symbol id="i-branch" viewBox="0 0 24 24">
        <circle cx="6.5" cy="5.5" r="2" />
        <circle cx="6.5" cy="18.5" r="2" />
        <circle cx="17.5" cy="8" r="2" />
        <path d="M6.5 7.5v9" />
        <path d="M17.5 10c0 3.6-3.3 4.4-11 6.5" />
      </symbol>
      <symbol id="i-inbox" viewBox="0 0 24 24">
        <path d="M3 13.5h5l1.5 2.5h5l1.5-2.5h5" />
        <path d="M5.6 5h12.8L21 13.5V18a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18v-4.5Z" />
      </symbol>
      <symbol id="i-plug" viewBox="0 0 24 24">
        <path d="M9 3v4.5M15 3v4.5" />
        <path d="M6.5 7.5h11V11a5.5 5.5 0 0 1-11 0Z" />
        <path d="M12 16.5V21" />
      </symbol>
      <symbol id="s-good" viewBox="0 0 24 24">
        <circle cx="12" cy="12" r="10" fill="currentColor" stroke="none" />
        <path d="m7.6 12.4 3 3 5.8-6.3" fill="none" />
      </symbol>
      <symbol id="s-warning" viewBox="0 0 24 24">
        <path
          d="M10.2 3.4a2.1 2.1 0 0 1 3.6 0l8.4 14.5a2.1 2.1 0 0 1-1.8 3.1H3.6a2.1 2.1 0 0 1-1.8-3.1Z"
          fill="currentColor"
          stroke="none"
        />
        <path d="M12 9v4.4M12 17.2h.01" fill="none" />
      </symbol>
      <symbol id="s-critical" viewBox="0 0 24 24">
        <circle cx="12" cy="12" r="10" fill="currentColor" stroke="none" />
        <path d="m8.7 8.7 6.6 6.6M15.3 8.7l-6.6 6.6" fill="none" />
      </symbol>
      <symbol id="s-info" viewBox="0 0 24 24">
        <circle cx="12" cy="12" r="10" fill="currentColor" stroke="none" />
        <path d="M7.8 12h.01M12 12h.01M16.2 12h.01" fill="none" />
      </symbol>
      <symbol id="s-neutral" viewBox="0 0 24 24">
        <circle
          cx="12"
          cy="12"
          r="8.6"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.4"
        />
        <path d="M8.6 12h6.8" fill="none" />
      </symbol>
    </svg>
  );
}
