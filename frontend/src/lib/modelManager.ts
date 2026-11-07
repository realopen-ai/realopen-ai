/** Pure presentation helpers shared by the model manager and its tests. */
export function modelBytes(size: number | null | undefined): string {
  if (size == null || !Number.isFinite(size) || size < 0) return "—";
  if (size < 1_000_000) return `${(size / 1_000).toFixed(0)} KB`;
  if (size < 1_000_000_000) return `${(size / 1_000_000).toFixed(1)} MB`;
  return `${(size / 1_000_000_000).toFixed(1)} GB`;
}
export function modelJobProgress(
  completed: number,
  total: number,
): number | null {
  return total > 0
    ? Math.min(100, Math.max(0, (completed / total) * 100))
    : null;
}
export function modelInstallName(
  catalog: string,
  model: string,
  variant: string,
): string {
  return catalog === "ollama"
    ? `${model}:${variant}`
    : `${model.replace(/\//g, "-").toLowerCase().slice(0, 120)}:${variant
        .replace(/[^a-zA-Z0-9_.-]/g, "-")
        .replace(/\.gguf$/i, "")
        .slice(-70)}`;
}
export function activeModelJob(status: string): boolean {
  return status === "queued" || status === "running";
}
