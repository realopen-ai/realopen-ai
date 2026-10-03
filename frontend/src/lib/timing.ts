/** Normalize legacy epoch-seconds and current epoch-milliseconds timestamps. */
export function epochMilliseconds(
  value: number | null | undefined,
  fallback = Date.now(),
): number {
  if (value == null || !Number.isFinite(value) || value <= 0) return fallback;
  return value < 100_000_000_000 ? value * 1000 : value;
}

/** Safe elapsed time: invalid or reversed clocks never become negative UI. */
export function elapsedMilliseconds(
  startedAt: number | null | undefined,
  completedAt: number | null | undefined,
): number | null {
  if (startedAt == null || completedAt == null) return null;
  const start = epochMilliseconds(startedAt, Number.NaN);
  const end = epochMilliseconds(completedAt, Number.NaN);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start)
    return null;
  return end - start;
}
