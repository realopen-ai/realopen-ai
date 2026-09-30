import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/**
 * Shared visual primitives.
 *
 * These establish the app-wide rhythm:
 *  - PageHeader: title (22px semibold) + optional description + actions
 *  - SectionHeader: section titles (15px medium) with optional action
 *  - EmptyState: quiet, intentional empty states
 *  - StatusDot: colored dot + label (replaces pill badges for statuses)
 *  - PageContainer: deliberate content width per page type
 *
 * Use these instead of re-deriving layout in each screen.
 */

/* ── Page container ──────────────────────────────────────────── */

type PageWidth = "narrow" | "list" | "wide" | "full";

const widthClass: Record<PageWidth, string> = {
  /** Settings-like pages (~880px) */
  narrow: "max-w-[880px]",
  /** Brain lists (~1080px) */
  list: "max-w-[1080px]",
  /** Workspace grids (~1200px) */
  wide: "max-w-[1200px]",
  /** Coding workspace etc. */
  full: "max-w-none",
};

export function PageContainer({
  width = "list",
  className,
  children,
}: {
  width?: PageWidth;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div
      className={cn(
        "mx-auto w-full px-6 lg:px-10 py-8 lg:py-10",
        widthClass[width],
        className,
      )}
    >
      {children}
    </div>
  );
}

/* ── Page header ─────────────────────────────────────────────── */

export function PageHeader({
  title,
  description,
  actions,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn("flex items-start justify-between gap-4 mb-8", className)}
    >
      <div className="min-w-0">
        <h1 className="text-[22px] font-semibold tracking-[-0.01em] text-foreground leading-tight">
          {title}
        </h1>
        {description && (
          <p className="mt-1.5 text-[14px] text-muted-foreground leading-relaxed">
            {description}
          </p>
        )}
      </div>
      {actions && (
        <div className="flex items-center gap-2 shrink-0">{actions}</div>
      )}
    </div>
  );
}

/* ── Section header ──────────────────────────────────────────── */

export function SectionHeader({
  title,
  description,
  actions,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn("flex items-center justify-between gap-3 mb-4", className)}
    >
      <div className="min-w-0">
        <h2 className="text-[15px] font-medium text-foreground leading-snug">
          {title}
        </h2>
        {description && (
          <p className="mt-0.5 text-[13px] text-muted-foreground">
            {description}
          </p>
        )}
      </div>
      {actions && (
        <div className="flex items-center gap-1.5 shrink-0">{actions}</div>
      )}
    </div>
  );
}

/* ── Empty state ─────────────────────────────────────────────── */

export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
}: {
  icon?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center text-center py-16 px-6",
        className,
      )}
    >
      {icon && (
        <div className="mb-4 flex h-11 w-11 items-center justify-center rounded-xl bg-secondary text-muted-foreground [&_svg]:size-5">
          {icon}
        </div>
      )}
      <p className="text-[15px] font-medium text-foreground">{title}</p>
      {description && (
        <p className="mt-1.5 max-w-105 text-[13.5px] text-muted-foreground leading-relaxed">
          {description}
        </p>
      )}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

/* ── Status dot ──────────────────────────────────────────────── */

type StatusTone = "success" | "warning" | "danger" | "neutral" | "running";

const toneDot: Record<StatusTone, string> = {
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-danger",
  neutral: "bg-muted-foreground/50",
  running: "bg-success",
};

const toneText: Record<StatusTone, string> = {
  success: "text-muted-foreground",
  warning: "text-muted-foreground",
  danger: "text-danger",
  neutral: "text-muted-foreground",
  running: "text-muted-foreground",
};

export function StatusDot({
  tone = "neutral",
  label,
  pulse = false,
  className,
}: {
  tone?: StatusTone;
  label?: ReactNode;
  pulse?: boolean;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 text-xs",
        toneText[tone],
        className,
      )}
    >
      <span className="relative flex h-1.75 w-1.75 shrink-0">
        {pulse && (
          <span
            className={cn(
              "absolute inline-flex h-full w-full rounded-full opacity-60 animate-ping",
              toneDot[tone],
            )}
          />
        )}
        <span
          className={cn(
            "relative inline-flex h-1.75 w-1.75 rounded-full",
            toneDot[tone],
            pulse && "animate-pulse",
          )}
        />
      </span>
      {label && <span className="truncate">{label}</span>}
    </span>
  );
}

/* ── Filter chip (compact, interactive) ─────────────────────── */

export function FilterChip({
  active,
  children,
  className,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { active?: boolean }) {
  return (
    <button
      type="button"
      className={cn(
        "inline-flex h-7 items-center rounded-md px-2.5 text-[12.5px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60",
        active
          ? "bg-primary/15 text-primary"
          : "text-muted-foreground hover:bg-surface-hover hover:text-foreground",
        className,
      )}
      {...props}
    >
      {children}
    </button>
  );
}

/* ── List row separator style helper ────────────────────────── */

export const listRowClass =
  "group/row flex items-center gap-3 rounded-lg px-3 py-2.5 transition-colors hover:bg-surface-hover";

export const listSeparatorClass = "divide-y divide-border/50";
