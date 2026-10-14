/** Interpret schema-loading diagnostics, including earlier persisted results. */
export function toolAccessDetails(output?: string) {
  const names = (value: unknown): string[] =>
    Array.isArray(value)
      ? value.filter((name) => typeof name === "string")
      : [];
  try {
    const data = JSON.parse(output || "null");
    if (data && typeof data === "object" && !Array.isArray(data)) {
      const loaded = names(data.loaded);
      const alreadyLoaded = names(data.already_loaded);
      const requested = names(data.requested);
      return {
        deferred: data.operation === "deferred_call",
        requested: requested.length ? requested : [...loaded, ...alreadyLoaded],
        loaded,
        alreadyLoaded,
      };
    }
  } catch {}
  const legacy = output?.match(/^Loaded ([\w-]+)\. The requested action/);
  return {
    deferred: false,
    requested: legacy ? [legacy[1]] : [],
    loaded: legacy ? [legacy[1]] : [],
    alreadyLoaded: [] as string[],
  };
}
