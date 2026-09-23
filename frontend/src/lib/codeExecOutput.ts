type CommandResult = {
  stdout?: unknown;
  stderr?: unknown;
};

function asText(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** Turn sandbox command envelopes into the terminal text users care about. */
export function formatCodeExecOutput(output: unknown): string {
  if (typeof output !== "string") return "";

  const trimmed = output.trim();
  if (!trimmed.startsWith("{") || !trimmed.endsWith("}")) return output;

  try {
    const result = JSON.parse(trimmed) as CommandResult;
    if (!result || typeof result !== "object") return output;

    const stdout = asText(result.stdout);
    const stderr = asText(result.stderr);
    if (!stdout && !stderr) return output;
    if (!stdout) return stderr;
    if (!stderr) return stdout;
    return `${stdout.replace(/\s+$/, "")}\n${stderr}`;
  } catch {
    return output;
  }
}
