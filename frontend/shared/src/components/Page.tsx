import type { ComponentProps, ReactNode } from "react";

/**
 * Page title with optional actions. No subtitle: put context in the page.
 * - back: a link (rendered by the app's router) shown above the title.
 * - meta: something small next to the title, such as a status badge.
 */
export function PageHeader({
  title,
  back,
  meta,
  actions,
  children,
}: {
  title: ReactNode;
  back?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  /** Rare: one line under the title, e.g. an app's public URL. */
  children?: ReactNode;
}) {
  return (
    <header className="ui-page-header">
      {back && <div className="ui-page-header__back">{back}</div>}
      <div className="ui-page-header__row">
        <div className="ui-page-header__title">
          <h1>{title}</h1>
          {meta}
        </div>
        {actions && <div className="ui-page-header__actions">{actions}</div>}
      </div>
      {children && <div className="ui-page-header__extra">{children}</div>}
    </header>
  );
}

/** Class for a "back" link above a PageHeader title. */
export const backLinkClass = "ui-back-link";

/** Content width for a page: "wide" (default) or "narrow" for single forms. */
export function Page({
  width = "wide",
  className = "",
  children,
}: {
  width?: "wide" | "narrow";
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={`ui-page ui-page--${width} ${className}`.trim()}>
      {children}
    </div>
  );
}

/**
 * A titled card. Sections stack with consistent spacing inside a Page.
 * - flush: remove inner padding, for tables and lists that run edge to edge.
 * - footer: right-aligned actions on a quiet strip, for forms.
 */
export function Section({
  title,
  actions,
  footer,
  flush = false,
  className = "",
  headingLevel = 2,
  children,
  ...props
}: Omit<ComponentProps<"section">, "title"> & {
  title?: ReactNode;
  actions?: ReactNode;
  footer?: ReactNode;
  flush?: boolean;
  headingLevel?: 2 | 3;
}) {
  const Heading = headingLevel === 2 ? "h2" : "h3";
  return (
    <section {...props} className={`ui-card ui-section ${className}`.trim()}>
      {(title || actions) && (
        <div className="ui-section__header">
          {title && <Heading className="ui-section__title">{title}</Heading>}
          {actions && <div className="ui-section__actions">{actions}</div>}
        </div>
      )}
      <div
        className={
          flush
            ? "ui-section__body ui-section__body--flush"
            : "ui-section__body"
        }
      >
        {children}
      </div>
      {footer && <div className="ui-section__footer">{footer}</div>}
    </section>
  );
}

/** Plain bordered surface without a header. */
export function Card({ className = "", ...props }: ComponentProps<"div">) {
  return (
    <div {...props} className={`ui-card ui-card--padded ${className}`.trim()} />
  );
}

const gaps = { 1: "1", 2: "2", 3: "3", 4: "4", 6: "6", 8: "8" } as const;
type Gap = keyof typeof gaps;

/** Vertical rhythm. gap is a spacing-scale step. */
export function Stack({
  gap = 4,
  className = "",
  ...props
}: ComponentProps<"div"> & { gap?: Gap }) {
  return (
    <div
      {...props}
      className={`ui-stack ui-gap-${gaps[gap]} ${className}`.trim()}
    />
  );
}

/** Horizontal row that wraps, e.g. a group of buttons. */
export function Cluster({
  gap = 2,
  justify = "start",
  className = "",
  ...props
}: ComponentProps<"div"> & {
  gap?: Gap;
  justify?: "start" | "end" | "between";
}) {
  return (
    <div
      {...props}
      className={`ui-cluster ui-gap-${gaps[gap]} ui-justify-${justify} ${className}`.trim()}
    />
  );
}

/** Responsive columns that collapse to one column on phones. */
export function Grid({
  columns = 2,
  gap = 4,
  className = "",
  ...props
}: ComponentProps<"div"> & { columns?: 2 | 3; gap?: Gap }) {
  return (
    <div
      {...props}
      className={`ui-grid ui-grid--${columns} ui-gap-${gaps[gap]} ${className}`.trim()}
    />
  );
}

/**
 * Centered single column for signed-out flows (sign in, account setup):
 * a title, one card and an optional footer line.
 */
export function AuthLayout({
  title,
  mark,
  footer,
  children,
}: {
  title: ReactNode;
  /** Brand mark shown above the title. */
  mark?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="ui-auth">
      <div className="ui-auth__header">
        {mark && (
          <span
            className="ui-brand__mark ui-brand__mark--lg"
            aria-hidden="true"
          >
            {mark}
          </span>
        )}
        <h1>{title}</h1>
      </div>
      <div className="ui-card ui-card--padded ui-stack ui-gap-4">
        {children}
      </div>
      {footer && <div className="ui-auth__footer">{footer}</div>}
    </div>
  );
}
