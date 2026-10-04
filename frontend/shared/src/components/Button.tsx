import type { ComponentProps, ReactNode } from "react";
import { Icon, type IconName } from "../Icon";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md";

/**
 * Class names for anything that should look like a button, including router
 * links: <Link className={buttonClass({ variant: "primary" })}>.
 */
export function buttonClass({
  variant = "secondary",
  size = "md",
  block = false,
}: { variant?: ButtonVariant; size?: ButtonSize; block?: boolean } = {}) {
  return [
    "ui-button",
    `ui-button--${variant}`,
    size === "sm" && "ui-button--sm",
    block && "ui-button--block",
  ]
    .filter(Boolean)
    .join(" ");
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span
      className="ui-spinner"
      role={label ? "status" : undefined}
      aria-label={label}
    >
      <span aria-hidden="true" />
    </span>
  );
}

type ButtonProps = Omit<ComponentProps<"button">, "className"> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  block?: boolean;
  /** Disables the button and shows a spinner. Keep the label a verb. */
  loading?: boolean;
  icon?: IconName;
  className?: string;
  children: ReactNode;
};

/** Defaults to type="button"; pass type="submit" inside forms. */
export function Button({
  variant,
  size,
  block,
  loading = false,
  icon,
  className = "",
  type = "button",
  disabled,
  children,
  ...props
}: ButtonProps) {
  return (
    <button
      {...props}
      type={type}
      className={`${buttonClass({ variant, size, block })} ${className}`.trim()}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
    >
      {loading ? <Spinner /> : icon ? <Icon name={icon} /> : null}
      {children}
    </button>
  );
}

/** Square icon-only button. The label is required and becomes its name. */
export function IconButton({
  icon,
  label,
  className = "",
  type = "button",
  ...props
}: Omit<ComponentProps<"button">, "children" | "aria-label"> & {
  icon: IconName;
  label: string;
}) {
  return (
    <button
      {...props}
      type={type}
      className={`ui-icon-button ${className}`.trim()}
      aria-label={label}
      title={label}
    >
      <Icon name={icon} />
    </button>
  );
}
