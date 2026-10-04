import type { ComponentProps, ReactNode } from "react";

export function Card({ className = "", ...props }: ComponentProps<"div">) {
  return <div className={`${className} card`.trim()} {...props} />;
}
export function ActivityRow({
  className = "operation",
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return <li className={className}>{children}</li>;
}
export function ShellFrame({
  brand,
  actions,
  children,
  footer,
  beforeMain,
  afterMain,
  mainClass = "page",
}: {
  brand: ReactNode;
  actions: ReactNode;
  children: ReactNode;
  footer: ReactNode;
  beforeMain?: ReactNode;
  afterMain?: ReactNode;
  mainClass?: string;
}) {
  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <div className="topbar-inner">
          {brand}
          <div className="topbar-actions">{actions}</div>
        </div>
      </header>
      {beforeMain}
      <main id="main" className={mainClass} tabIndex={-1}>
        {children}
      </main>
      <footer className="footer">{footer}</footer>
      {afterMain}
    </>
  );
}
