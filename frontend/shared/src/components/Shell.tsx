import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Icon } from "../Icon";

/** Class for a primary navigation link; pass whether it is the current section. */
export function navLinkClass(active: boolean) {
  return active ? "ui-nav__link ui-nav__link--active" : "ui-nav__link";
}

/**
 * Skip link, sticky header and main region.
 * - brand: the home link with the platform mark and name.
 * - nav: primary links (use navLinkClass and aria-current="page").
 * - actions: compact header controls such as the theme toggle and account menu.
 * - mobileNav: optional content for the phone menu; defaults to nav.
 * - subnav: optional section tabs rendered under the header.
 */
export function AppShell({
  brand,
  nav,
  actions,
  mobileNav,
  subnav,
  mainClassName = "",
  children,
}: {
  brand: ReactNode;
  nav?: ReactNode;
  actions?: ReactNode;
  mobileNav?: ReactNode;
  subnav?: ReactNode;
  mainClassName?: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const header = useRef<HTMLElement>(null);
  const phoneNav = mobileNav ?? nav;
  useEffect(() => {
    if (!open) return;
    function close(event: KeyboardEvent | MouseEvent) {
      if (
        event instanceof KeyboardEvent
          ? event.key === "Escape"
          : !header.current?.contains(event.target as Node)
      )
        setOpen(false);
    }
    document.addEventListener("keydown", close);
    document.addEventListener("mousedown", close);
    return () => {
      document.removeEventListener("keydown", close);
      document.removeEventListener("mousedown", close);
    };
  }, [open]);
  return (
    <div className="ui-shell">
      <a className="ui-skip-link" href="#main">
        Skip to content
      </a>
      <header className="ui-header" ref={header}>
        <div className="ui-header__inner">
          <div className="ui-header__brand">{brand}</div>
          {nav && (
            <nav className="ui-nav ui-header__nav" aria-label="Main">
              {nav}
            </nav>
          )}
          <div className="ui-header__actions">
            {actions}
            {phoneNav && (
              <button
                type="button"
                className="ui-icon-button ui-header__menu-button"
                aria-expanded={open}
                aria-controls={panelId}
                aria-label="Menu"
                onClick={() => setOpen(!open)}
              >
                <Icon name={open ? "x" : "menu"} />
              </button>
            )}
          </div>
        </div>
        {phoneNav && (
          <nav
            id={panelId}
            className="ui-header__panel"
            aria-label="Main"
            hidden={!open}
            onClick={(event) => {
              if ((event.target as HTMLElement).closest("a, button"))
                setOpen(false);
            }}
          >
            {phoneNav}
          </nav>
        )}
      </header>
      {subnav && <div className="ui-subnav">{subnav}</div>}
      <main
        id="main"
        tabIndex={-1}
        className={`ui-main ${mainClassName}`.trim()}
      >
        {children}
      </main>
    </div>
  );
}

/** Brand lockup: mark plus the configured platform name. */
export function Brand({ name, mark }: { name?: ReactNode; mark: ReactNode }) {
  return (
    <>
      <span className="ui-brand__mark">{mark}</span>
      {name && <span className="ui-brand__name">{name}</span>}
    </>
  );
}

/**
 * Header popover for the signed-in account. The trigger shows initials; the
 * panel holds identity details and actions such as Sign out. Closes on
 * Escape, outside click or choosing an action, and returns focus.
 */
export function AccountMenu({
  name,
  detail,
  badge,
  children,
}: {
  name: string;
  detail?: ReactNode;
  badge?: ReactNode;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const panelId = useId();
  useEffect(() => {
    if (!open) return;
    function close(event: KeyboardEvent | MouseEvent) {
      if (event instanceof KeyboardEvent) {
        if (event.key !== "Escape") return;
        setOpen(false);
        trigger.current?.focus();
      } else if (!root.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("keydown", close);
    document.addEventListener("mousedown", close);
    return () => {
      document.removeEventListener("keydown", close);
      document.removeEventListener("mousedown", close);
    };
  }, [open]);
  const initials =
    name
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((part) => part[0]!.toUpperCase())
      .join("") || "?";
  return (
    <div className="ui-account" ref={root}>
      <button
        ref={trigger}
        type="button"
        className="ui-account__trigger"
        aria-expanded={open}
        aria-controls={panelId}
        aria-label={`Account: ${name}`}
        onClick={() => setOpen(!open)}
      >
        <span className="ui-avatar" aria-hidden="true">
          {initials}
        </span>
      </button>
      <div
        id={panelId}
        className="ui-account__panel"
        hidden={!open}
        onClick={(event) => {
          if ((event.target as HTMLElement).closest("a, button"))
            setOpen(false);
        }}
      >
        <div className="ui-account__identity">
          <span className="ui-account__name">{name}</span>
          {detail && <span className="ui-account__detail">{detail}</span>}
          {badge}
        </div>
        <div className="ui-account__actions">{children}</div>
      </div>
    </div>
  );
}

/** Class for a full-width row action inside AccountMenu or the phone menu. */
export const menuItemClass = "ui-menu-item";
