import { useEffect, useState } from "react";
import { Code2, Pause, Play, Square } from "lucide-react";
import { Button } from "@/components/ui/button";
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
  return (
    <section
      aria-label={t("coderTask.title")}
      className="mx-auto mb-2 w-full max-w-210 px-4 sm:px-6 text-xs"
    >
      <div className="rounded-xl border border-border/60 bg-card px-3 py-2.5">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <span className="inline-flex items-center gap-2 font-medium">
            <Code2 className="h-4 w-4 text-primary" />
            {t("coderTask.title")}
          </span>
          <span className="text-muted-foreground">
            {t(`coderTask.status.${task.status}`)} ·{" "}
            {coderTaskDuration(elapsed)}
          </span>
          {progress.round != null && (
            <span>
              {t("coderTask.round", {
                round: String(progress.round),
                max: String(progress.max_rounds ?? "?"),
              })}
            </span>
          )}
          {percent != null && (
            <span title={t("coderTask.contextHelp")}>
              {t("coderTask.context", { percent: String(percent) })}
            </span>
          )}
          <div className="ms-auto flex flex-wrap gap-1">
            {running && (
              <>
                <Button
                  size="xs"
                  variant="ghost"
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
                  size="xs"
                  variant="ghost"
                  disabled={pending || task.cancellation_requested}
                  onClick={() => void act("cancel")}
                >
                  <Square />
                  {t("coderTask.cancel")}
                </Button>
              </>
            )}
            {!running && task.can_resume && (
              <Button
                size="xs"
                disabled={pending || streaming}
                onClick={() => void act("resume")}
              >
                <Play />
                {t("coderTask.resume")}
              </Button>
            )}
          </div>
        </div>
        {running && (
          <p className="mt-1.5 text-muted-foreground">
            {t(`coderTask.phase.${progress.phase || "preparing"}`)}
            {progress.phase === "tool" && progress.action
              ? ` · ${progress.action.replace(/_/g, " ")}`
              : ""}
          </p>
        )}
        {task.stop_after_round && running && (
          <p className="mt-1.5 text-muted-foreground">
            {t("coderTask.stopHelp")}
          </p>
        )}
        <details className="mt-2 text-muted-foreground">
          <summary className="cursor-pointer hover:text-foreground">
            {t("coderTask.details")}
          </summary>
          <div className="mt-2 space-y-2 break-words">
            <p>{task.request}</p>
            {progress.model && <p>{progress.model}</p>}
            {task.resumed_from && (
              <p>
                {t("coderTask.continued", {
                  id: task.resumed_from.slice(0, 8),
                })}
              </p>
            )}
            {percent != null && (
              <p>
                {t("coderTask.tokens", {
                  used: String(progress.input_tokens),
                  limit: String(progress.input_limit),
                  output: String(progress.output_reserve),
                })}
              </p>
            )}
            <p>{t("coderTask.resumeHelp")}</p>
            {task.files_changed.length > 0 && (
              <div>
                <p className="font-medium text-foreground">
                  {t("coderTask.files")}
                </p>
                <ul className="list-inside list-disc">
                  {task.files_changed.map((file) => (
                    <li key={file}>{file}</li>
                  ))}
                </ul>
              </div>
            )}
            {task.plan?.steps?.length ? (
              <div>
                <p className="font-medium text-foreground">
                  {t("coderTask.plan")}
                </p>
                <ul className="space-y-1">
                  {task.plan.steps.map((step, index) => (
                    <li key={index}>
                      <span aria-hidden="true">
                        {step.status === "completed"
                          ? "✓"
                          : step.status === "in_progress"
                            ? "→"
                            : "○"}
                      </span>{" "}
                      {step.task}
                    </li>
                  ))}
                </ul>
                {task.plan.next_action && (
                  <p className="mt-1">{task.plan.next_action}</p>
                )}
              </div>
            ) : null}
            {Object.keys(task.verification).length > 0 && (
              <div>
                <p className="font-medium text-foreground">
                  {t("coderTask.verification")}
                </p>
                <ul className="list-inside list-disc">
                  {Object.entries(task.verification).map(([name, check]) => (
                    <li key={name}>
                      {name.replace(/_/g, " ")}:{" "}
                      {t(`coderTask.check.${check.status}`)}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {!running && task.summary && (
              <p className="whitespace-pre-wrap max-h-48 overflow-y-auto">
                {task.summary}
              </p>
            )}
          </div>
        </details>
        {error && (
          <p role="alert" className="mt-2 text-destructive">
            {error}
          </p>
        )}
      </div>
    </section>
  );
}
