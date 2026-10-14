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
        readRequired: data.operation === "read_required",
        requiredTools: names(data.required_tools),
        requested: requested.length ? requested : [...loaded, ...alreadyLoaded],
        loaded,
        alreadyLoaded,
      };
    }
  } catch {}
  // Earlier persisted warnings contained the internal repair prompt. Present
  // the same concise notice on reload without displaying those instructions.
  if (output?.startsWith("This update_artifact call was NOT executed.")) {
    return {
      deferred: false,
      readRequired: true,
      requiredTools: ["read_artifact"],
      requested: ["update_artifact"],
      loaded: [] as string[],
      alreadyLoaded: [] as string[],
    };
  }
  const legacy = output?.match(/^Loaded ([\w-]+)\. The requested action/);
  return {
    deferred: false,
    readRequired: false,
    requiredTools: [] as string[],
    requested: legacy ? [legacy[1]] : [],
    loaded: legacy ? [legacy[1]] : [],
    alreadyLoaded: [] as string[],
  };
}
