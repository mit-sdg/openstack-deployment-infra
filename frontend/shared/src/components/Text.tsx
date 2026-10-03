import { useState } from "react";
import { Icon } from "../Icon";

/** "just now", "5 minutes ago", "in 2 hours", "3 days ago". */
export function relativeTime(value: string, now = Date.now()) {
  const seconds = Math.round((new Date(value).getTime() - now) / 1000);
  if (Math.abs(seconds) < 60) return "just now";
  const formatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  if (Math.abs(seconds) < 3600)
    return formatter.format(Math.trunc(seconds / 60), "minute");
  if (Math.abs(seconds) < 86400)
    return formatter.format(Math.trunc(seconds / 3600), "hour");
  return formatter.format(Math.trunc(seconds / 86400), "day");
}

/**
 * Relative time in a <time> element; the full local date and time is the
 * tooltip. Missing values show `empty` in the subtle color.
 */
export function RelativeTime({
  value,
  empty = "—",
}: {
  value: string | null | undefined;
  empty?: string;
}) {
  if (!value) return <span className="ui-text-subtle">{empty}</span>;
  return (
    <time dateTime={value} title={new Date(value).toLocaleString()}>
      {relativeTime(value)}
    </time>
  );
}

/**
 * The start of a long ID in monospace, with a button that copies the whole
 * value. The full ID is also the tooltip. `label` names what is copied,
 * e.g. "owner ID", and becomes the button's accessible name.
 */
export function CopyId({
  value,
  label,
  length = 8,
}: {
  value: string;
  label: string;
  /** Characters shown. Commits use 9 to match the rest of the portal. */
  length?: number;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <span className="ui-copy-id">
      <code title={value}>{value.slice(0, length)}</code>
      <button
        type="button"
        className="ui-copy-id__button"
        aria-label={`Copy ${label}`}
        title={`Copy ${label}`}
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(value);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 2000);
          } catch {
            /* Clipboard denied: the full ID stays available in the tooltip. */
          }
        }}
      >
        <Icon name={copied ? "check" : "copy"} />
      </button>
      <span className="ui-sr-only" role="status">
        {copied ? "Copied to clipboard" : ""}
      </span>
    </span>
  );
}
