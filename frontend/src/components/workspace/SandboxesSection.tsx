import { useEffect, useState } from "react";
import {
  ArrowUpRight,
  Box,
  Loader2,
  MoreHorizontal,
  Play,
  Plus,
  RotateCw,
  Square,
  Trash2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { EmptyState, StatusDot } from "@/components/ui/primitives";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useUIStore } from "@/store/uiStore";
import { useSandboxStore, type Sandbox } from "@/store/sandboxStore";
import {
  CreateSandboxDialog,
  DeleteSandboxDialog,
} from "@/components/workspace/SandboxDialogs";
import { useT } from "@/store/settingsStore";

function formatBytesShort(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "—";
  if (bytes < 1073741824)
    return `${Math.max(1, Math.round(bytes / 1048576))} MB`;
  return `${(bytes / 1073741824).toFixed(1)} GB`;
}

function formatRam(memoryMb: number): string {
  if (!Number.isFinite(memoryMb) || memoryMb <= 0) return "—";
  if (memoryMb < 1024) return `${memoryMb} MB`;
  const gb = memoryMb / 1024;
  return `${Number.isInteger(gb) ? gb : gb.toFixed(1)} GB`;
}

function relativeActivity(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const time = Date.parse(iso);
  if (!Number.isFinite(time)) return null;
  const diff = Date.now() - time;
  if (diff < 60e3) return "just now";
  const minutes = Math.floor(diff / 60e3);
  if (minutes < 60) return `${minutes} min${minutes !== 1 ? "s" : ""} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hour${hours !== 1 ? "s" : ""} ago`;
  const days = Math.floor(hours / 24);
  return `${days} day${days !== 1 ? "s" : ""} ago`;
}

export function SandboxesSection() {
  const t = useT();
  const {
    sandboxes,
    loading,
    error,
    lifecyclePending,
    loadSandboxes,
    lifecycle,
    activateSandbox,
  } = useSandboxStore();
  const setRightPanelOpen = useUIStore((s) => s.setRightPanelOpen);
  const [createOpen, setCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<
    (typeof sandboxes)[number] | null
  >(null);

  useEffect(() => {
    void loadSandboxes();
  }, [loadSandboxes]);

  const openSandbox = (item: Sandbox) => {
    void activateSandbox(item.id);
    setRightPanelOpen(true);
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto w-full max-w-300 px-6 pb-16 pt-6 lg:px-10">
        <div className="mb-5 flex items-center justify-between gap-3">
          <p className="text-xs text-muted-foreground">
            {t("workspace.count", { count: sandboxes.length })}
          </p>
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus />
            {t("workspace.createSandbox")}
          </Button>
        </div>

        {error && (
          <p className="mb-4 rounded-lg bg-danger/10 px-3.5 py-2.5 text-[13px] text-danger">
            {error}
          </p>
        )}

        {loading ? (
          <div className="flex items-center justify-center py-16 text-muted-foreground">
            <Loader2 className="h-5 w-5 animate-spin" />
          </div>
        ) : sandboxes.length === 0 ? (
          <EmptyState
            icon={<Box />}
            title={t("workspace.emptySandboxes")}
            description={t("workspace.emptySandboxesDescription")}
            action={
              <Button size="sm" onClick={() => setCreateOpen(true)}>
                <Plus />
                {t("workspace.createSandbox")}
              </Button>
            }
            className="rounded-xl"
          />
        ) : (
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {sandboxes.map((item) => {
              const pendingAction = lifecyclePending[item.id];
              const isPending = Boolean(pendingAction);
              const isRunning = item.status === "running";
              const hasQuota =
                Number.isFinite(item.workspace_quota_bytes) &&
                item.workspace_quota_bytes > 0 &&
                Number.isFinite(item.usage_bytes);
              const usagePercent = hasQuota
                ? Math.min(
                    100,
                    (item.usage_bytes / item.workspace_quota_bytes) * 100,
                  )
                : 0;
              const lastActive = relativeActivity(
                (item as Sandbox & { last_active?: string | null }).last_active,
              );
              return (
                <div
                  key={item.id}
                  className="flex flex-col rounded-xl border border-border/60 bg-card p-5 transition-colors hover:bg-surface-hover"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <h3 className="truncate text-[15px] font-medium text-foreground">
                        {item.name}
                      </h3>
                      <div className="mt-1.5">
                        {isPending ? (
                          <StatusDot
                            tone="warning"
                            label={
                              <span className="capitalize">
                                {pendingAction}…
                              </span>
                            }
                          />
                        ) : (
                          <StatusDot
                            tone={isRunning ? "running" : "neutral"}
                            pulse={isRunning}
                            label={
                              isRunning
                                ? t("workspace.status.running")
                                : t("workspace.status.stopped")
                            }
                          />
                        )}
                      </div>
                    </div>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon-sm"
                          aria-label={`Actions for ${item.name}`}
                        >
                          <MoreHorizontal />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem
                          disabled={isPending}
                          onSelect={() => void lifecycle("restart", item.id)}
                        >
                          <RotateCw />
                          {t("workspace.restart")}
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          disabled={isPending}
                          onSelect={() =>
                            void lifecycle(
                              isRunning ? "stop" : "start",
                              item.id,
                            )
                          }
                        >
                          {isRunning ? <Square /> : <Play />}
                          {isRunning
                            ? t("workspace.stop")
                            : t("workspace.start")}
                        </DropdownMenuItem>
                        <DropdownMenuSeparator />
                        <DropdownMenuItem
                          className="text-danger focus:text-danger [&_svg]:text-danger"
                          // Defer one tick so the dropdown layer fully
                          // unmounts before the dialog mounts (Radix
                          // pointer-events leak workaround).
                          onSelect={() =>
                            window.setTimeout(() => setDeleteTarget(item), 0)
                          }
                        >
                          <Trash2 />
                          {t("workspace.delete")}
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>

                  <div className="mt-4">
                    <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground">
                      <span className="shrink-0">
                        {item.cpu_limit} CPU · {formatRam(item.memory_limit_mb)}{" "}
                        RAM
                      </span>
                      {hasQuota && (
                        <span className="tabular-nums">
                          {formatBytesShort(item.usage_bytes)} /{" "}
                          {formatBytesShort(item.workspace_quota_bytes)}{" "}
                          {t("workspace.used")}
                        </span>
                      )}
                    </div>
                    {hasQuota && (
                      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-secondary">
                        <div
                          className="h-full rounded-full bg-primary transition-all"
                          style={{ width: `${usagePercent}%` }}
                        />
                      </div>
                    )}
                    {lastActive && (
                      <p className="mt-2.5 text-xs text-muted-foreground">
                        Last active {lastActive}
                      </p>
                    )}
                  </div>

                  <div className="mt-auto flex items-center justify-end pt-4">
                    <Button
                      size="sm"
                      variant={isRunning ? "default" : "outline"}
                      disabled={isPending}
                      onClick={() => openSandbox(item)}
                    >
                      {isPending ? (
                        <Loader2 className="animate-spin" />
                      ) : (
                        <ArrowUpRight />
                      )}
                      {t("workspace.open")}
                    </Button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
      <CreateSandboxDialog open={createOpen} onOpenChange={setCreateOpen} />
      <DeleteSandboxDialog
        sandbox={deleteTarget}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
      />
    </div>
  );
}
