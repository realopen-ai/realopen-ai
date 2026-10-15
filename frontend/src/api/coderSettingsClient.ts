export interface CoderPreferences {
  context_window: number;
  generation_limit: number;
  max_rounds: number;
  thinking: boolean;
  temperature: number;
}
export interface CoderSettingsResponse {
  settings: CoderPreferences;
  defaults: CoderPreferences;
  safety_margin: number;
  model_context_overrides: Record<string, number>;
}
async function request(
  method: string,
  values?: CoderPreferences,
  signal?: AbortSignal,
): Promise<CoderSettingsResponse> {
  const response = await fetch("/api/coder/settings", {
    method,
    signal,
    ...(values
      ? {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(values),
        }
      : {}),
  });
  if (!response.ok)
    throw new Error(`Coder settings request failed: ${response.status}`);
  return response.json();
}
export const fetchCoderSettings = (signal?: AbortSignal) =>
  request("GET", undefined, signal);
export const saveCoderSettings = (values: CoderPreferences) =>
  request("PUT", values);
export const resetCoderSettings = () => request("DELETE");
export function validateCoderPreferences(
  values: CoderPreferences,
  response: Pick<
    CoderSettingsResponse,
    "safety_margin" | "model_context_overrides"
  >,
): "range" | "budget" | null {
  if (
    typeof values.thinking !== "boolean" ||
    !Number.isFinite(values.temperature) ||
    values.temperature < 0 ||
    values.temperature > 2
  )
    return "range";
  for (const [value, min, max] of [
    [values.context_window, 2048, 524288],
    [values.generation_limit, 128, 65536],
    [values.max_rounds, 4, 96],
  ]) {
    if (!Number.isInteger(value) || value < min || value > max) return "range";
  }
  return values.generation_limit + response.safety_margin >=
    Math.min(
      values.context_window,
      ...Object.values(response.model_context_overrides),
    )
    ? "budget"
    : null;
}
