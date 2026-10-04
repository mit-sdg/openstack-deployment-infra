import type { ReactNode } from "react";

export type Tone = "good" | "warning" | "critical" | "info" | "neutral";
export function StatusBadge({
  label,
  tone,
  indicator,
}: {
  label: string;
  tone: Tone;
  indicator?: ReactNode;
}) {
  return (
    <span className={`status tone-${tone}`} data-tone={tone}>
      {indicator ?? <span className="status-dot" aria-hidden="true" />}
      {label}
    </span>
  );
}
