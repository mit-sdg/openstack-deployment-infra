import { BoundaryText, StatusBadge } from "@openstack-platform/ui";
import { ToneIcon } from "./Icons";
import type { Status, History } from "./snapshot";

const absoluteFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "medium",
});
export const clockFormat = new Intl.DateTimeFormat(undefined, {
  timeStyle: "medium",
});
export const numberFormat = new Intl.NumberFormat();
export function parseTime(value: string | null | undefined) {
  if (typeof value !== "string") return null;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}
export function relative(value: string | null | undefined, now = Date.now()) {
  const time = parseTime(value);
  if (time === null) return "never";
  const seconds = Math.max(0, Math.round((now - time) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds} s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}
export function absolute(value: string | null | undefined) {
  const time = parseTime(value);
  return time === null ? "Unknown time" : absoluteFormat.format(time);
}
export function Time({
  value,
  prefix = "",
  now = Date.now(),
}: {
  value: string | null | undefined;
  prefix?: string;
  now?: number;
}) {
  return parseTime(value) === null ? (
    <span className="muted">—</span>
  ) : (
    <time dateTime={value!} title={absolute(value)}>
      {prefix}
      {relative(value, now)}
    </time>
  );
}
export function Badge({ status }: { status: Status }) {
  return (
    <StatusBadge
      label={status.label}
      tone={status.tone}
      indicator={<ToneIcon tone={status.tone} />}
    />
  );
}
export function sentence(text: string | null | undefined) {
  return typeof text === "string" && /^[a-z]+(?:\s|$)/.test(text)
    ? text[0].toUpperCase() + text.slice(1)
    : text;
}
export function plural(count: number, singular: string) {
  return `${numberFormat.format(count)} ${count === 1 ? singular : singular + "s"}`;
}
export function trimNumber(value: number) {
  return Number.isInteger(value)
    ? String(value)
    : value.toFixed(1).replace(/\.0$/, "");
}
export function formatCpu(value: number | null | undefined) {
  return typeof value === "number"
    ? value >= 1000
      ? `${trimNumber(value / 1000)} GHz`
      : `${value} MHz`
    : null;
}
export function formatMemory(value: number | null | undefined) {
  return typeof value === "number"
    ? value >= 1024
      ? `${trimNumber(value / 1024)} GiB`
      : `${value} MiB`
    : null;
}
export function formatBytes(bytes: number | null | undefined) {
  if (typeof bytes !== "number") return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let value = bytes,
    index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index++;
  }
  return `${trimNumber(Math.round(value * 10) / 10)} ${units[index]}`;
}
export function safeHref(url: string | null | undefined) {
  try {
    const parsed = new URL(url ?? "");
    return parsed.protocol === "https:" ? parsed.href : null;
  } catch {
    return null;
  }
}
export function hostOf(url: string) {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}
export function ExternalLink({
  url,
  label,
  className = "",
}: {
  url: string | null | undefined;
  label: string;
  className?: string;
}) {
  const href = safeHref(url);
  return href ? (
    <a
      className={`ext-link ${className}`}
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(event) => event.stopPropagation()}
    >
      <BoundaryText text={label} />
      <span className="ext-link__tail"> ↗</span>
    </a>
  ) : (
    <span className={className}>{label}</span>
  );
}
export function HistoryBars({
  checks,
  label,
}: {
  checks: History[];
  label: string;
}) {
  const recent = checks.slice(-24),
    passed = recent.filter((x) => x.tone === "good").length,
    failed = recent.filter((x) => x.tone === "critical").length;
  return (
    <span
      className="bars"
      role="img"
      aria-label={
        recent.length
          ? `${label}: last ${plural(recent.length, "check")}, ${passed} passed, ${failed} failed`
          : `${label}: no checks recorded yet`
      }
    >
      {Array.from({ length: 24 - recent.length }, (_, i) => (
        <span className="bar" key={`empty-${i}`} />
      ))}
      {recent.map((check, i) => (
        <span
          className="bar"
          key={`${check.at}-${i}`}
          data-tone={check.tone}
          title={`${({ good: "Passed", warning: "Unverified", critical: "Failed" } as Record<string, string>)[check.tone] ?? "Unknown"} · ${absolute(check.at)}`}
        />
      ))}
    </span>
  );
}
