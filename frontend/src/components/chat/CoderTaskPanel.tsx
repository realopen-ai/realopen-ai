import { useEffect, useState } from "react";
import {
  Check,
  ChevronRight,
  Circle,
  Code2,
  Loader2,
  Pause,
  Play,
  Square,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { useT } from "@/store/settingsStore";
import {
  listCoderTasks,
  controlCoderTask,
  type CoderTask,
} from "@/api/coderTasksClient";
import {
  activeCoderTask,
  coderContextPercent,
  coderTaskDuration,
} from "@/lib/coderTaskProgress";

export function CoderTaskPanel({
  conversationId,
  streaming,
  onResume,
}: {
  conversationId: string | null;
  streaming: boolean;
  onResume: (id: string) => Promise<void>;
}) {
  const t = useT();
  const [task, setTask] = useState<CoderTask | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    setTask(null);
    setError("");
    if (!conversationId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        const data = await listCoderTasks(conversationId!, controller.signal);
        if (controller.signal.aborted) return;
        setTask(data.tasks[0] ?? null);
        setError("");
        timer = setTimeout(
          refresh,
          streaming || data.tasks.some(activeCoderTask) ? 2000 : 10000,
        );
      } catch (cause) {
        if (!controller.signal.aborted) {
          setError(cause instanceof Error ? cause.message : String(cause));
          timer = setTimeout(refresh, 5000);
        }
      }
    }
    void refresh();
    const clock = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      controller.abort();
      clearTimeout(timer);
      clearInterval(clock);
    };
  }, [conversationId, streaming]);
  useEffect(() => {
    setPending(false);
  }, [task?.id]);
  if (!task || !conversationId) return null;
  const running = activeCoderTask(task);
  const progress = task.progress;
  const percent = coderContextPercent(progress);
  const elapsed = running
    ? Math.max(0, (now - Date.parse(task.created_at)) / 1000)
    : task.elapsed_seconds;
  async function act(action: "stop-after-round" | "cancel" | "resume") {
    if (!task || !conversationId) return;
    setPending(true);
    setError("");
    try {
      if (action === "resume") await onResume(task.id);
      else setTask(await controlCoderTask(conversationId, task.id, action));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setPending(false);
    }
  }
  const status = t(`coderTask.status.${task.status}`);
  const round =
    progress.round == null
      ? null
      : t("coderTask.round", {
          round: String(progress.round),
          max: String(progress.max_rounds ?? "?"),
        });
  const statusTone = running
    ? "bg-primary/10 text-primary"
    : task.status === "completed"
      ? "bg-success/10 text-success"
      : task.status === "failed"
        ? "bg-danger/10 text-danger"
        : "bg-secondary text-muted-foreground";

  return (
    <Dialog>
      <section
        aria-label={t("coderTask.title")}
        className="rounded-xl border border-border/60 bg-card text-sm"
      >
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 p-1.5">
          <DialogTrigger asChild>
            <button
              type="button"
              className="flex min-w-0 grow basis-44 items-center gap-2.5 rounded-lg px-2 py-1 text-start transition-colors hover:bg-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <Code2
                aria-hidden="true"
                className="size-4 shrink-0 text-muted-foreground"
              />
              <span className="min-w-0 grow">
                <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="font-medium">{t("coderTask.title")}</span>
                  <span
                    className={cn(
                      "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium",
                      statusTone,
                    )}
                  >
                    {running && (
                      <Loader2
                        aria-hidden="true"
                        className="size-3 motion-safe:animate-spin"
                      />
                    )}
                    {status}
                  </span>
                  <span className="text-xs tabular-nums text-muted-foreground">
                    {coderTaskDuration(elapsed)}
                  </span>
                </span>
                {running && (
                  <span className="mt-1 block truncate text-xs text-muted-foreground">
                    {[
                      round,
                      t(`coderTask.phase.${progress.phase || "preparing"}`),
                      progress.phase === "tool"
                        ? progress.action?.replace(/_/g, " ")
                        : null,
                      percent == null
                        ? null
                        : t("coderTask.context", { percent: String(percent) }),
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </span>
                )}
              </span>
              <ChevronRight
                aria-hidden="true"
                className="size-4 shrink-0 text-muted-foreground rtl:rotate-180"
              />
              <span className="sr-only">{t("coderTask.details")}</span>
            </button>
          </DialogTrigger>
          {running && (
            <div className="flex flex-wrap items-center gap-1 px-1">
              <Button
                type="button"
                size="xs"
                variant="secondary"
                disabled={
                  pending ||
                  task.stop_after_round ||
                  task.cancellation_requested
                }
                onClick={() => void act("stop-after-round")}
              >
                <Pause />
                {t(
                  task.stop_after_round
                    ? "coderTask.stopping"
                    : "coderTask.stopAfterRound",
                )}
              </Button>
              <Button
                type="button"
                size="xs"
                variant="ghost"
                className="text-danger"
                disabled={pending || task.cancellation_requested}
                onClick={() => void act("cancel")}
              >
                <Square />
                {t("coderTask.cancel")}
              </Button>
            </div>
          )}
          {!running && task.can_resume && (
            <Button
              type="button"
              size="xs"
              disabled={pending || streaming}
              onClick={() => void act("resume")}
            >
              <Play />
              {t("coderTask.resume")}
            </Button>
          )}
        </div>
        {task.stop_after_round && running && (
          <p className="px-4 pb-2 text-xs text-muted-foreground">
            {t("coderTask.stopHelp")}
          </p>
        )}
        {error && (
          <p role="alert" className="break-words px-4 pb-2 text-xs text-danger">
            {error}
          </p>
        )}
      </section>

      <DialogContent className="flex max-h-[80dvh] max-w-xl flex-col gap-0 overflow-hidden p-0">
        <DialogHeader className="shrink-0 border-b border-border/60 px-5 py-4 pe-12">
          <DialogTitle className="text-base font-semibold">
            {t("coderTask.title")}
          </DialogTitle>
          <DialogDescription className="text-sm text-muted-foreground">
            {[status, coderTaskDuration(elapsed), round, progress.model]
              .filter(Boolean)
              .join(" · ")}
          </DialogDescription>
        </DialogHeader>
        <div className="min-h-0 space-y-5 overflow-y-auto overscroll-contain px-5 py-4 text-sm [overflow-wrap:anywhere]">
          {!running && task.summary && (
            <p className="whitespace-pre-wrap rounded-lg bg-secondary/60 p-3 leading-relaxed">
              {task.summary}
            </p>
          )}
          {task.plan?.steps?.length ? (
            <section className="space-y-2">
              <h3 className="font-medium">{t("coderTask.plan")}</h3>
              <ol className="divide-y divide-border/60 rounded-lg border border-border/60">
                {task.plan.steps.map((step, index) => (
                  <li
                    key={index}
                    className="flex items-start gap-2.5 px-3 py-2.5"
                  >
                    {step.status === "completed" ? (
                      <Check
                        aria-hidden="true"
                        className="mt-0.5 size-4 shrink-0 text-success"
                      />
                    ) : step.status === "in_progress" ? (
                      <ChevronRight
                        aria-hidden="true"
                        className="mt-0.5 size-4 shrink-0 text-primary rtl:rotate-180"
                      />
                    ) : (
                      <Circle
                        aria-hidden="true"
                        className="mt-0.5 size-4 shrink-0 text-muted-foreground"
                      />
                    )}
                    <span>{step.task}</span>
                  </li>
                ))}
              </ol>
              {task.plan.next_action && (
                <p className="leading-relaxed text-muted-foreground">
                  {task.plan.next_action}
                </p>
              )}
            </section>
          ) : null}
          {Object.keys(task.verification).length > 0 && (
            <section className="space-y-2">
              <h3 className="font-medium">{t("coderTask.verification")}</h3>
              <ul className="divide-y divide-border/60 rounded-lg border border-border/60">
                {Object.entries(task.verification).map(([name, check]) => (
                  <li
                    key={name}
                    className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 px-3 py-2"
                  >
                    <span className="capitalize">
                      {name.replace(/_/g, " ")}
                    </span>
                    <span
                      className={cn(
                        "text-xs",
                        check.status === "passed"
                          ? "text-success"
                          : check.status === "failed"
                            ? "text-danger"
                            : "text-muted-foreground",
                      )}
                    >
                      {t(`coderTask.check.${check.status}`)}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          )}
          {task.files_changed.length > 0 && (
            <details className="rounded-lg border border-border/60">
              <summary className="cursor-pointer px-3 py-2.5 font-medium">
                {t("coderTask.files")}{" "}
                <span className="ms-1 text-xs text-muted-foreground">
                  ({task.files_changed.length})
                </span>
              </summary>
              <ul className="divide-y divide-border/60 border-t border-border/60 font-mono text-xs">
                {task.files_changed.map((file) => (
                  <li
                    key={file}
                    dir="ltr"
                    className="px-3 py-2 text-muted-foreground"
                  >
                    {file}
                  </li>
                ))}
              </ul>
            </details>
          )}
          <details className="rounded-lg border border-border/60">
            <summary className="cursor-pointer px-3 py-2.5 font-medium">
              {t("coderTask.request")}
            </summary>
            <p className="whitespace-pre-wrap border-t border-border/60 px-3 py-2.5 leading-relaxed">
              {task.request}
            </p>
          </details>
          {task.resumed_from && (
            <p className="text-xs text-muted-foreground">
              {t("coderTask.continued", { id: task.resumed_from.slice(0, 8) })}
            </p>
          )}
          {percent != null && (
            <p
              className="text-xs text-muted-foreground"
              title={t("coderTask.contextHelp")}
            >
              {t("coderTask.tokens", {
                used: String(progress.input_tokens),
                limit: String(progress.input_limit),
                output: String(progress.output_reserve),
              })}
            </p>
          )}
          {task.can_resume && (
            <p className="text-xs leading-relaxed text-muted-foreground">
              {t("coderTask.resumeHelp")}
            </p>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
