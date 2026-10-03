import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Icon, type IconName } from "../Icon";

export type Tone = "neutral" | "info" | "success" | "warning" | "danger";

/** Short status label. Tone is never the only signal: the text says it. */
export function Badge({
  tone = "neutral",
  dot = true,
  children,
}: {
  tone?: Tone;
  dot?: boolean;
  children: ReactNode;
}) {
  return (
    <span className={`ui-badge ui-tone-${tone}`} data-tone={tone}>
      {dot && <span className="ui-badge__dot" aria-hidden="true" />}
      {children}
    </span>
  );
}

const alertIcons: Record<Tone, IconName> = {
  neutral: "info",
  info: "info",
  success: "success",
  warning: "warning",
  danger: "danger",
};

/**
 * Banner for something the person should notice. Say what happened and
 * what to do. Danger alerts are announced immediately (role="alert").
 */
export function Alert({
  tone = "info",
  title,
  action,
  focusOnMount = false,
  children,
}: {
  tone?: Tone;
  title?: ReactNode;
  action?: ReactNode;
  /** Move focus here when it appears, e.g. a form submission error. */
  focusOnMount?: boolean;
  children?: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (focusOnMount) ref.current?.focus();
  }, [focusOnMount]);
  return (
    <div
      ref={ref}
      tabIndex={focusOnMount ? -1 : undefined}
      className={`ui-alert ui-tone-${tone}`}
      role={tone === "danger" ? "alert" : "status"}
    >
      <Icon name={alertIcons[tone]} className="ui-alert__icon" />
      <div className="ui-alert__body">
        {title && <p className="ui-alert__title">{title}</p>}
        {children && <div className="ui-alert__text">{children}</div>}
      </div>
      {action && <div className="ui-alert__action">{action}</div>}
    </div>
  );
}

/** Renders nothing without an error; otherwise a focused danger Alert with its message. */
export function ErrorAlert({
  error,
  focus = true,
}: {
  error: unknown;
  focus?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (error && focus) ref.current?.focus();
  }, [error, focus]);
  if (!error) return null;
  return (
    <div
      ref={ref}
      tabIndex={-1}
      className="ui-alert ui-tone-danger"
      role="alert"
    >
      <Icon name="danger" className="ui-alert__icon" />
      <div className="ui-alert__body">
        <div className="ui-alert__text">
          {error instanceof Error ? error.message : String(error)}
        </div>
      </div>
    </div>
  );
}

/** Small inline result next to the control that caused it, e.g. "Saved". */
export function InlineStatus({
  tone = "success",
  children,
}: {
  tone?: Tone;
  children: ReactNode;
}) {
  return (
    <p className={`ui-inline-status ui-tone-${tone}`} role="status">
      <Icon name={alertIcons[tone]} />
      <span>{children}</span>
    </p>
  );
}

/** Placeholder for an empty list or a page with nothing to show yet. */
export function EmptyState({
  title,
  icon = "box",
  action,
  children,
}: {
  title: ReactNode;
  icon?: IconName;
  action?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="ui-empty">
      <span className="ui-empty__icon">
        <Icon name={icon} />
      </span>
      <h2 className="ui-empty__title">{title}</h2>
      {children && <div className="ui-empty__text">{children}</div>}
      {action && <div className="ui-empty__action">{action}</div>}
    </div>
  );
}

/** Loading placeholder. Pick the shape closest to the content that will load. */
export function Skeleton({
  variant = "text",
  width = "full",
}: {
  variant?: "text" | "title" | "block" | "circle";
  width?: "quarter" | "half" | "three-quarters" | "full";
}) {
  return (
    <span
      className={`ui-skeleton ui-skeleton--${variant} ui-skeleton--${width}`}
      aria-hidden="true"
    />
  );
}

/** Announced loading region with a few skeleton rows. */
export function LoadingRows({
  rows = 3,
  label = "Loading…",
}: {
  rows?: number;
  label?: string;
}) {
  return (
    <div className="ui-loading-rows" role="status" aria-busy="true">
      <span className="ui-sr-only">{label}</span>
      {Array.from({ length: rows }, (_, index) => (
        <div className="ui-loading-rows__row" key={index}>
          <Skeleton width={index % 2 ? "half" : "three-quarters"} />
          <Skeleton width="quarter" />
        </div>
      ))}
    </div>
  );
}

/** Whole-page placeholder: a title bar and a card of rows. */
export function PageSkeleton({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="ui-page-skeleton" role="status" aria-busy="true">
      <span className="ui-sr-only">{label}</span>
      <Skeleton variant="title" width="quarter" />
      <div
        className="ui-card ui-card--padded ui-stack ui-gap-4"
        aria-hidden="true"
      >
        <Skeleton width="three-quarters" />
        <Skeleton width="half" />
        <Skeleton width="three-quarters" />
      </div>
    </div>
  );
}

type Toast = { id: number; tone: Tone; message: ReactNode };
const ToastContext = createContext<
  ((message: ReactNode, tone?: Tone) => void) | null
>(null);

/** Hosts transient confirmations. Wrap the app once. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const next = useRef(0);
  const dismiss = useCallback(
    (id: number) =>
      setToasts((current) => current.filter((toast) => toast.id !== id)),
    [],
  );
  const show = useCallback(
    (message: ReactNode, tone: Tone = "success") => {
      const id = ++next.current;
      setToasts((current) => [...current.slice(-2), { id, tone, message }]);
      window.setTimeout(() => dismiss(id), 5000);
    },
    [dismiss],
  );
  return (
    <ToastContext.Provider value={show}>
      {children}
      <div className="ui-toasts" role="status" aria-live="polite">
        {toasts.map((toast) => (
          <div key={toast.id} className={`ui-toast ui-tone-${toast.tone}`}>
            <Icon name={alertIcons[toast.tone]} className="ui-toast__icon" />
            <span className="ui-toast__message">{toast.message}</span>
            <button
              type="button"
              className="ui-toast__close"
              aria-label="Dismiss"
              onClick={() => dismiss(toast.id)}
            >
              <Icon name="x" />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

/**
 * Returns toast(message, tone?). Use for confirmations that do not need
 * action ("Variable saved"). Errors that need action belong in an Alert.
 */
export function useToast() {
  const show = useContext(ToastContext);
  if (!show) throw new Error("useToast needs a ToastProvider");
  return show;
}
