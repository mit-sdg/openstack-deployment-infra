import {
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
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
   * - "title" leads the card; "trailing" sits opposite it (e.g. a status).
   * - "secondary" is a muted line under the title, without a label
   *   (e.g. a username). "meta" is a smaller, subtle line (e.g. a time).
   * - "field" (default) is a labelled line; "hidden" is omitted.
   * Prefer secondary/meta when a row has only one or two extra values.
   */
  mobile?: "title" | "trailing" | "secondary" | "meta" | "field" | "hidden";
};

/**
 * Table on wide screens, stacked rows on phones. Explicit ARIA roles keep
 * the table semantics when CSS changes the display of rows and cells.
 *
 * With onRowClick the whole row is clickable (hover state and a chevron).
 * Keep a real link in the "title" column: it is the keyboard and screen
 * reader target and supports opening in a new tab. On phones that link
 * stretches over the whole stacked card, so the card is one tap target.
 */
export function DataTable<T>({
  label,
  columns,
  rows,
  rowKey,
  empty,
  onRowClick,
}: {
  /** Accessible name, e.g. "Apps". */
  label: string;
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  /** Shown instead of the table when there are no rows. */
  empty?: ReactNode;
  onRowClick?: (row: T) => void;
}) {
  if (!rows.length && empty) return <>{empty}</>;
  return (
    <div className="ui-table-wrap">
      <table
        className={onRowClick ? "ui-table ui-table--clickable" : "ui-table"}
        role="table"
        aria-label={label}
      >
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
            {onRowClick && (
              <th
                role="columnheader"
                aria-hidden="true"
                className="ui-table__chevron-cell"
              />
            )}
          </tr>
        </thead>
        <tbody role="rowgroup">
          {rows.map((row) => (
            <tr
              role="row"
              key={rowKey(row)}
              onClick={
                onRowClick &&
                ((event) => {
                  // Links and controls inside the row keep their own behaviour,
                  // and selecting text does not navigate.
                  if (
                    (event.target as Element).closest(
                      "a, button, input, select, textarea, label",
                    )
                  )
                    return;
                  if (window.getSelection()?.toString()) return;
                  onRowClick(row);
                })
              }
            >
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
              {onRowClick && (
                <td
                  role="cell"
                  aria-hidden="true"
                  data-mobile="chevron"
                  className="ui-table__chevron-cell"
                >
                  <Icon name="chevron-right" className="ui-table__chevron" />
                </td>
              )}
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
  density = "default",
  children,
}: {
  label?: string;
  /** "compact" puts each row's title and facts on one line, for feeds. */
  density?: "default" | "compact";
  children: ReactNode;
}) {
  return (
    <ul
      className={density === "compact" ? "ui-list ui-list--compact" : "ui-list"}
      aria-label={label}
    >
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

/** Text values longer than this stack under their label on phones. */
const inlineLimit = 28;

/**
 * Label/value pairs. Wide screens: a label column and a value column.
 * Phones: label left and value right on one line, like stacked table
 * cards; long text values (or items with `stacked`) put the value under the
 * label instead.
 */
export function KeyValueList({
  items,
  columns = 1,
}: {
  items: {
    label: ReactNode;
    value: ReactNode;
    key?: string;
    /** Force the stacked phone layout, e.g. for long links or paragraphs. */
    stacked?: boolean;
  }[];
  columns?: 1 | 2;
}) {
  return (
    <dl className={`ui-kv ui-kv--${columns}`}>
      {items.map((item, index) => {
        const stacked =
          item.stacked ??
          (typeof item.value === "string" && item.value.length > inlineLimit);
        return (
          <div
            className={
              stacked ? "ui-kv__item ui-kv__item--stacked" : "ui-kv__item"
            }
            key={item.key ?? index}
          >
            <dt>{item.label}</dt>
            <dd>{item.value}</dd>
          </div>
        );
      })}
    </dl>
  );
}

/**
 * Monospaced text block. "log" caps the height and scrolls; it is focusable
 * so keyboard users can scroll it. Text is rendered as text, never HTML.
 * `end` shows the end first (and again when the text changes), for logs
 * whose newest lines matter most.
 */
export function CodeBlock({
  label,
  variant = "block",
  end = false,
  children,
}: {
  label: string;
  variant?: "block" | "log";
  end?: boolean;
  children: string;
}) {
  const ref = useRef<HTMLPreElement>(null);
  useLayoutEffect(() => {
    if (end && ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [end, children]);
  return (
    <pre
      ref={ref}
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
