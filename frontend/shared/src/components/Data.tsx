import { useId, useState, type ReactNode } from "react";
import { Icon } from "../Icon";
import { Field } from "./Field";

export type Column<T> = {
  key: string;
  /** Plain text: also used as the row label on phones. */
  header: string;
  cell: (row: T) => ReactNode;
  /** Hide the header text visually (it stays for screen readers). */
  hideHeader?: boolean;
  align?: "start" | "end";
  /**
   * Phone layout, where each row becomes a stacked card:
   * "title" leads the card, "trailing" sits opposite it (e.g. a status),
   * "field" (default) is a label/value line, "hidden" is omitted.
   */
  mobile?: "title" | "trailing" | "field" | "hidden";
};

/**
 * Table on wide screens, stacked rows on phones. Explicit ARIA roles keep
 * the table semantics when CSS changes the display of rows and cells.
 */
export function DataTable<T>({
  label,
  columns,
  rows,
  rowKey,
  empty,
}: {
  /** Accessible name, e.g. "Apps". */
  label: string;
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  /** Shown instead of the table when there are no rows. */
  empty?: ReactNode;
}) {
  if (!rows.length && empty) return <>{empty}</>;
  return (
    <div className="ui-table-wrap">
      <table className="ui-table" role="table" aria-label={label}>
        <thead role="rowgroup">
          <tr role="row">
            {columns.map((column) => (
              <th
                key={column.key}
                role="columnheader"
                scope="col"
                className={column.align === "end" ? "ui-align-end" : undefined}
              >
                {column.hideHeader ? (
                  <span className="ui-sr-only">{column.header}</span>
                ) : (
                  column.header
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody role="rowgroup">
          {rows.map((row) => (
            <tr role="row" key={rowKey(row)}>
              {columns.map((column) => (
                <td
                  key={column.key}
                  role="cell"
                  data-label={column.hideHeader ? undefined : column.header}
                  data-mobile={column.mobile ?? "field"}
                  className={
                    column.align === "end" ? "ui-align-end" : undefined
                  }
                >
                  {column.cell(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Divided list for feeds and simple collections. */
export function List({
  label,
  children,
}: {
  label?: string;
  children: ReactNode;
}) {
  return (
    <ul className="ui-list" aria-label={label}>
      {children}
    </ul>
  );
}

/**
 * One list row: title, an optional quiet meta line (facts, not prose),
 * optional extra content and trailing items such as a badge or button.
 */
export function ListItem({
  title,
  meta,
  trailing,
  children,
}: {
  title: ReactNode;
  meta?: ReactNode;
  trailing?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <li className="ui-list__item">
      <div className="ui-list__main">
        <div className="ui-list__title">{title}</div>
        {meta && <div className="ui-list__meta">{meta}</div>}
        {children}
      </div>
      {trailing && <div className="ui-list__trailing">{trailing}</div>}
    </li>
  );
}

/** Label/value pairs. Two columns on wide screens, stacked on phones. */
export function KeyValueList({
  items,
  columns = 1,
}: {
  items: { label: ReactNode; value: ReactNode; key?: string }[];
  columns?: 1 | 2;
}) {
  return (
    <dl className={`ui-kv ui-kv--${columns}`}>
      {items.map((item, index) => (
        <div className="ui-kv__item" key={item.key ?? index}>
          <dt>{item.label}</dt>
          <dd>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * Monospaced text block. "log" caps the height and scrolls; it is focusable
 * so keyboard users can scroll it. Text is rendered as text, never HTML.
 */
export function CodeBlock({
  label,
  variant = "block",
  children,
}: {
  label: string;
  variant?: "block" | "log";
  children: string;
}) {
  return (
    <pre
      className={`ui-code ui-code--${variant}`}
      tabIndex={0}
      aria-label={label}
    >
      {children}
    </pre>
  );
}

/** Read-only value with a copy button. Not for secrets that must stay hidden. */
export function CopyField({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: ReactNode;
}) {
  const [copied, setCopied] = useState(false);
  const id = useId();
  return (
    <Field label={label} hint={hint} id={id}>
      <span className="ui-input-group">
        <input
          id={id}
          className="ui-input ui-input-group__control ui-mono"
          value={value}
          readOnly
          onFocus={(event) => event.target.select()}
        />
        <button
          type="button"
          className="ui-input-group__button ui-input-group__button--text"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(value);
              setCopied(true);
              window.setTimeout(() => setCopied(false), 2000);
            } catch {
              (
                document.getElementById(id) as HTMLInputElement | null
              )?.select();
            }
          }}
        >
          <Icon name={copied ? "check" : "copy"} />
          <span>{copied ? "Copied" : "Copy"}</span>
        </button>
      </span>
      <span className="ui-sr-only" role="status">
        {copied ? "Copied to clipboard" : ""}
      </span>
    </Field>
  );
}
