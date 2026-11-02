export type CoderCheck = {
  label: string;
  command?: string;
  status: "passed" | "failed" | "unknown";
  exitCode?: number;
  output?: string;
};

export type CoderToolPresentation = {
  summary: string;
  files: string[];
  checks: CoderCheck[];
  facts: Array<{ label: string; value: string }>;
  stdout?: string;
  stderr?: string;
  plan?: {
    steps: Array<{
      task: string;
      status: "pending" | "in_progress" | "completed";
    }>;
    nextAction: string;
    notes?: string;
    files: string[];
  };
};

type JsonRecord = Record<string, unknown>;

function record(value: unknown): JsonRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : null;
}

function stringValue(value: unknown): string | undefined {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" || typeof value === "boolean")
    return String(value);
  return undefined;
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value)
    ? value
    : undefined;
}

function parseRecord(value: unknown): JsonRecord | null {
  if (record(value)) return record(value);
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (!trimmed.startsWith("{") || !trimmed.endsWith("}")) return null;
  try {
    return record(JSON.parse(trimmed));
  } catch {
    return null;
  }
}

function humanize(value: string): string {
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function compactText(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const nested = parseRecord(value);
  if (nested) {
    const stdout = stringValue(nested.stdout);
    const stderr = stringValue(nested.stderr);
    return [stdout, stderr].filter(Boolean).join("\n").trim() || undefined;
  }
  return value.trim() || undefined;
}

function statusFor(recordValue: JsonRecord): CoderCheck["status"] {
  const exitCode = numberValue(recordValue.exit_code ?? recordValue.exitCode);
  const status = stringValue(recordValue.status)?.toLowerCase();
  if (exitCode === 0 || status === "passed" || status === "success")
    return "passed";
  if (
    (exitCode !== undefined && exitCode !== 0) ||
    status === "failed" ||
    status === "error"
  )
    return "failed";
  return "unknown";
}

function checkFrom(value: unknown, index: number): CoderCheck | null {
  const item = record(value);
  if (!item) return null;
  const nestedResult = parseRecord(item.result ?? item.output);
  const exitCode = numberValue(
    item.exit_code ?? item.exitCode ?? nestedResult?.exit_code,
  );
  const action = stringValue(item.action);
  const command = stringValue(item.command);
  return {
    label: action ? humanize(action) : `Check ${index + 1}`,
    command,
    status: statusFor({ ...item, ...(nestedResult ?? {}) }),
    exitCode,
    output:
      compactText(item.result ?? item.output) ??
      compactText(nestedResult?.stdout) ??
      compactText(nestedResult?.stderr),
  };
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.flatMap((item) => (typeof item === "string" ? [item] : []))
    : [];
}

function addFact(
  facts: CoderToolPresentation["facts"],
  label: string,
  value: unknown,
) {
  const text = stringValue(value);
  if (text !== undefined) facts.push({ label, value: text });
}

/**
 * Build a display-only view of a coder result. The original JSON is never
 * changed: it remains available to the model, persistence, and traces.
 */
export function presentCoderToolOutput(
  output: unknown,
  options: { toolName?: string; title?: string; exitCode?: number } = {},
): CoderToolPresentation | null {
  const data = parseRecord(output);
  if (!data) return null;

  const savedPlan = record(data.saved_plan);
  if (
    savedPlan &&
    Array.isArray(savedPlan.steps) &&
    savedPlan.steps.length &&
    typeof savedPlan.next_action === "string" &&
    savedPlan.next_action.trim()
  ) {
    const steps: NonNullable<CoderToolPresentation["plan"]>["steps"] = [];
    for (const value of savedPlan.steps) {
      const step = record(value);
      if (
        !step ||
        typeof step.task !== "string" ||
        !step.task.trim() ||
        !["pending", "in_progress", "completed"].includes(String(step.status))
      )
        return null;
      steps.push({
        task: step.task,
        status: step.status as (typeof steps)[number]["status"],
      });
    }
    return {
      summary: `Plan saved · ${steps.length} step${steps.length === 1 ? "" : "s"}`,
      files: [],
      checks: [],
      facts: [],
      plan: {
        steps,
        nextAction: savedPlan.next_action,
        notes:
          typeof savedPlan.notes === "string" ? savedPlan.notes : undefined,
        files: stringArray(savedPlan.files),
      },
    };
  }

  const files = [
    ...stringArray(data.created_files),
    ...stringArray(data.changed_files),
    ...stringArray(data.files_changed),
    ...stringArray(data.files),
  ].filter((path, index, all) => all.indexOf(path) === index);
  const verification = record(data.verification);
  const verificationChecks = record(verification?.checks);
  const checks = [
    ...(Array.isArray(data.setup) ? data.setup : []),
    ...(Array.isArray(data.checks) ? data.checks : []),
    ...Object.entries(verificationChecks ?? {}).map(([name, value]) => ({
      ...(record(value) ?? {}),
      action: name,
    })),
  ].flatMap((item, index) => {
    const check = checkFrom(item, index);
    return check ? [check] : [];
  });
  const facts: CoderToolPresentation["facts"] = [];
  const toolName = options.toolName ?? "";
  const project = record(data.project);
  const stdout = stringValue(data.stdout) ?? compactText(data.result);
  const stderr = stringValue(data.stderr);
  const exitCode = numberValue(
    data.exit_code ?? data.exitCode ?? options.exitCode,
  );

  if (project) {
    addFact(
      facts,
      "Backend",
      stringValue(project.backend) ?? record(project.backend)?.assets,
    );
    addFact(
      facts,
      "Frontend",
      stringValue(project.frontend) ?? record(project.frontend)?.assets,
    );
    addFact(facts, "Project", project.name);
    addFact(facts, "Styling", record(project.frontend)?.styling);
    addFact(facts, "Framework", record(project.frontend)?.framework);
  }
  const scaffold = record(data.scaffold);
  if (scaffold) {
    addFact(facts, "Scaffold", scaffold.generator);
    const features = record(scaffold.features);
    if (features)
      addFact(
        facts,
        "Features",
        Object.keys(features).map(humanize).join(", "),
      );
    const resources = stringArray(scaffold.resources);
    if (resources.length) addFact(facts, "Resources", resources.join(", "));
  }
  addFact(facts, "Path", data.path);
  addFact(facts, "Bytes written", data.bytes);
  addFact(facts, "Port", data.port);
  addFact(facts, "Command ID", data.command_id);
  addFact(facts, "Persistent", data.persistent);
  if (exitCode !== undefined) addFact(facts, "Exit code", exitCode);
  if (data.host_formatting_changed_content === true)
    facts.push({ label: "Host formatting", value: "Applied" });

  let summary = options.title || "Coder tool completed";
  if (files.length) {
    const verb = ["scaffold_project", "scaffold_crud_app"].includes(toolName)
      ? "Created"
      : "Changed";
    summary = `${verb} ${files.length} file${files.length === 1 ? "" : "s"}`;
  } else if (checks.length) {
    const passed = checks.filter((check) => check.status === "passed").length;
    summary = `${passed} of ${checks.length} checks passed`;
  } else if (toolName === "start_preview" || data.persistent === true) {
    summary = "Preview process started";
  } else if (typeof data.status === "string") {
    summary = data.status;
  } else if (exitCode !== undefined) {
    summary = exitCode === 0 ? "Command completed" : "Command failed";
  }

  // A JSON object without any useful presentation fields should continue to
  // use the existing renderer; this avoids misclassifying unrelated tools.
  if (!files.length && !checks.length && !facts.length && !stdout && !stderr)
    return null;

  return { summary, files, checks, facts, stdout, stderr };
}

function indent(text: string): string {
  return text
    .trimEnd()
    .split("\n")
    .map((line) => `  ${line}`)
    .join("\n");
}

/** Human-readable projection for the read-only coder terminal. */
export function formatCoderToolOutput(
  output: unknown,
  options: { toolName?: string; title?: string; exitCode?: number } = {},
): string {
  const view = presentCoderToolOutput(output, options);
  if (!view) return typeof output === "string" ? output : "";

  const lines = [`✓ ${view.summary}`];
  if (view.plan) {
    lines.push("Model-authored plan, not execution verification:");
    for (const [index, step] of view.plan.steps.entries()) {
      const status =
        step.status === "completed"
          ? "Completed"
          : step.status === "in_progress"
            ? "In progress"
            : "Pending";
      lines.push(`  ${index + 1}. [${status}] ${step.task}`);
    }
    lines.push("Next action:", indent(view.plan.nextAction));
    if (view.plan.notes)
      lines.push("Decisions and notes:", indent(view.plan.notes));
    if (view.plan.files.length)
      lines.push(
        "Working files:",
        ...view.plan.files.map((path) => `  ${path}`),
      );
  }
  if (view.files.length) {
    lines.push("Files:", ...view.files.map((path) => `  + ${path}`));
  }
  if (view.checks.length) {
    lines.push("Checks:");
    for (const check of view.checks) {
      const icon =
        check.status === "passed" ? "✓" : check.status === "failed" ? "✗" : "•";
      lines.push(`  ${icon} ${check.command || check.label}`);
      if (check.output) lines.push(indent(check.output));
    }
  }
  if (view.facts.length) {
    lines.push(...view.facts.map((fact) => `${fact.label}: ${fact.value}`));
  }
  if (view.stdout) lines.push("Output:", indent(view.stdout));
  if (view.stderr) lines.push("Errors:", indent(view.stderr));
  return lines.join("\n");
}
