import type { CoderTask } from "../api/coderTasksClient.ts";

export function activeCoderTask(task: Pick<CoderTask, "status">): boolean {
  return task.status === "running" || task.status === "queued";
}
export function coderTaskDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  return hours ? `${hours}h ${minutes}m` : `${minutes}m ${total % 60}s`;
}
export function coderContextPercent(
  progress: CoderTask["progress"],
): number | null {
  if (!progress.input_limit || progress.input_tokens == null) return null;
  return Math.min(
    100,
    Math.max(
      0,
      Math.round((progress.input_tokens / progress.input_limit) * 100),
    ),
  );
}
