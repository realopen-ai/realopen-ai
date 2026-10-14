export interface AgentPreferences {
  context_window: number;
  generation_limit: number;
  history_compaction: boolean;
  compaction_threshold: number;
  recent_turns: number;
  tool_calling: "auto" | "text";
  repair_attempts: number;
}

export interface AgentSettingsResponse {
  settings: AgentPreferences;
  defaults: AgentPreferences;
  model_context_overrides: Record<string, number>;
  safety_margin: number;
}

async function request(
  method: string,
  settings?: AgentPreferences,
  signal?: AbortSignal,
): Promise<AgentSettingsResponse> {
  const response = await fetch("/api/agent/settings", {
    method,
    ...(settings
      ? {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(settings),
        }
      : {}),
    signal,
  });
  if (!response.ok)
    throw new Error(`Agent settings request failed: ${response.status}`);
  return response.json();
}

export const fetchAgentSettings = (signal?: AbortSignal) =>
  request("GET", undefined, signal);
export const saveAgentSettings = (settings: AgentPreferences) =>
  request("PUT", settings);
export const resetAgentSettings = () => request("DELETE");

export function validateAgentPreferences(
  value: AgentPreferences,
  response: AgentSettingsResponse,
): "range" | "budget" | null {
  const integers = [
    [value.context_window, 2048, 262144],
    [value.generation_limit, 128, 65536],
    [value.recent_turns, 0, 32],
    [value.repair_attempts, 0, 5],
  ];
  if (
    integers.some(
      ([n, min, max]) => !Number.isInteger(n) || n < min || n > max,
    ) ||
    !Number.isFinite(value.compaction_threshold) ||
    value.compaction_threshold < 0.5 ||
    value.compaction_threshold > 0.95 ||
    !["auto", "text"].includes(value.tool_calling) ||
    typeof value.history_compaction !== "boolean"
  )
    return "range";
  const smallestWindow = Math.min(
    value.context_window,
    ...Object.values(response.model_context_overrides),
  );
  return value.generation_limit + response.safety_margin >= smallestWindow
    ? "budget"
    : null;
}
