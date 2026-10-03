import { useEffect, useId, useRef, type ReactNode } from "react";
import { Icon } from "../Icon";

/**
 * Modal dialog built on <dialog>: focus is trapped, Escape closes, focus
 * returns to the opener. Centered on wide screens, a bottom sheet on phones.
 * Keep it controlled: open plus onClose.
 */
export function Dialog({
  open,
  onClose,
  title,
  footer,
  size = "md",
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  /** Actions, primary last. */
  footer?: ReactNode;
  size?: "sm" | "md" | "lg";
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    } else if (!open && dialog.open) {
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
    }
  }, [open]);
  return (
    <dialog
      ref={ref}
      className={`ui-dialog ui-dialog--${size}`}
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target === ref.current) onClose();
      }}
    >
      {open && (
        <div className="ui-dialog__panel">
          <div className="ui-dialog__header">
            <h2 id={titleId} className="ui-dialog__title">
              {title}
            </h2>
            <button
              type="button"
              className="ui-icon-button ui-icon-button--ghost"
              aria-label="Close"
              onClick={onClose}
            >
              <Icon name="x" />
            </button>
          </div>
          <div className="ui-dialog__body">{children}</div>
          {footer && <div className="ui-dialog__footer">{footer}</div>}
        </div>
      )}
    </dialog>
  );
}

/** Class for one router link inside TabNav. */
export function tabClass(active: boolean) {
  return active ? "ui-tabs__tab ui-tabs__tab--active" : "ui-tabs__tab";
}

/**
 * Page-level tabs made of links (each tab is a route). Mark the current one
 * with tabClass(true) and aria-current="page". Scrolls sideways on phones.
 */
export function TabNav({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <nav className="ui-tabs" aria-label={label}>
      {children}
    </nav>
  );
}

/**
 * Choose one of a few options in place (not navigation). Native radios, so
 * arrow keys and screen readers work as a radio group.
 */
export function SegmentedControl<T extends string>({
  label,
  hideLabel = false,
  name,
  options,
  value,
  onChange,
  block = false,
}: {
  label: string;
  hideLabel?: boolean;
  name: string;
  options: { value: T; label: ReactNode }[];
  value: T;
  onChange: (value: T) => void;
  block?: boolean;
}) {
  return (
    <fieldset
      className={block ? "ui-segmented ui-segmented--block" : "ui-segmented"}
    >
      <legend className={hideLabel ? "ui-sr-only" : "ui-field__label"}>
        {label}
      </legend>
      <div className="ui-segmented__track">
        {options.map((option) => (
          <label className="ui-segmented__option" key={option.value}>
            <input
              type="radio"
              name={name}
              value={option.value}
              checked={value === option.value}
              onChange={() => onChange(option.value)}
            />
            <span>{option.label}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}
