import { useEffect, useState } from "react";
import { ChevronDown, Loader2 } from "lucide-react";
import { useT } from "@/store/settingsStore";
import { Button } from "@/components/ui/button";
import { CoderAiSettings } from "./CoderAiSettings";
import {
  fetchAgentSettings,
  saveAgentSettings,
  resetAgentSettings,
  validateAgentPreferences,
  type AgentPreferences,
  type AgentSettingsResponse,
} from "@/api/agentSettingsClient";

const inputClass =
  "h-9 w-full rounded-lg border border-input bg-background px-3 text-[13px] text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60 disabled:opacity-50";

export function AdvancedAiSettings() {
  const t = useT();
  const [response, setResponse] = useState<AgentSettingsResponse | null>(null);
  const [draft, setDraft] = useState<AgentPreferences | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    fetchAgentSettings(controller.signal)
      .then((data) => {
        if (controller.signal.aborted) return;
        setResponse(data);
        setDraft(data.settings);
      })
      .catch(() => {
        if (!controller.signal.aborted) setError("loadError");
      });
    return () => controller.abort();
  }, [reload]);

  const update = <K extends keyof AgentPreferences>(
    key: K,
    value: AgentPreferences[K],
  ) => {
    setDraft((current) => current && { ...current, [key]: value });
    setError(null);
    setSaved(false);
  };

  const persist = async (reset = false) => {
    if (!draft || !response || busy) return;
    if (!reset) {
      const invalid = validateAgentPreferences(draft, response);
      if (invalid) {
        setError(invalid);
        return;
      }
    }
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const data = reset
        ? await resetAgentSettings()
        : await saveAgentSettings(draft);
      setResponse(data);
      setDraft(data.settings);
      setSaved(true);
    } catch {
      setError("saveError");
    } finally {
      setBusy(false);
    }
  };

  const numberField = (
    key:
      | "context_window"
      | "generation_limit"
      | "recent_turns"
      | "repair_attempts",
    min: number,
    max: number,
    label: string,
    hint: string,
  ) => (
    <label className="space-y-1.5 text-[13px]">
      <span className="block font-medium text-foreground">
        {t(`settings.ai.advanced.${label}`)}
      </span>
      <input
        type="number"
        required
        min={min}
        max={max}
        step={1}
        dir="ltr"
        className={inputClass}
        value={draft?.[key] ?? ""}
        disabled={busy}
        onChange={(e) => update(key, Number(e.target.value))}
      />
      <span className="block text-xs leading-relaxed text-muted-foreground">
        {t(`settings.ai.advanced.${hint}`)}
      </span>
    </label>
  );

  return (
    <details className="group rounded-xl border border-border/70">
      <summary className="flex cursor-pointer list-none items-center gap-3 rounded-xl px-4 py-3 text-[13.5px] font-medium text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60 [&::-webkit-details-marker]:hidden">
        {t("settings.ai.advanced.title")}
        <ChevronDown className="ms-auto h-4 w-4 text-muted-foreground transition-transform group-open:rotate-180" />
      </summary>
      <div className="space-y-5 border-t border-border/50 px-4 py-4">
        <p className="text-xs leading-relaxed text-muted-foreground">
          {t("settings.ai.advanced.description")}
        </p>
        {!draft && !error && (
          <Loader2
            className="h-4 w-4 animate-spin text-muted-foreground"
            aria-label={t("settings.ai.advanced.loading")}
          />
        )}
        {draft && (
          <form
            className="space-y-5"
            onSubmit={(e) => {
              e.preventDefault();
              void persist();
            }}
          >
            <fieldset disabled={busy} className="grid gap-4 sm:grid-cols-2">
              {numberField(
                "context_window",
                2048,
                262144,
                "context",
                "contextHint",
              )}
              {numberField(
                "generation_limit",
                128,
                65536,
                "generation",
                "generationHint",
              )}
            </fieldset>
            {!!Object.keys(response?.model_context_overrides ?? {}).length && (
              <p className="text-xs text-muted-foreground">
                {t("settings.ai.advanced.overridesHint")}
              </p>
            )}
            <fieldset
              disabled={busy}
              className="space-y-4 border-t border-border/50 pt-4"
            >
              <label className="flex items-start gap-2.5 text-[13px]">
                <input
                  type="checkbox"
                  checked={draft.history_compaction}
                  onChange={(e) =>
                    update("history_compaction", e.target.checked)
                  }
                  className="mt-0.5 h-4 w-4 accent-primary"
                />
                <span>
                  <span className="block font-medium text-foreground">
                    {t("settings.ai.advanced.compaction")}
                  </span>
                  <span className="mt-1 block text-xs leading-relaxed text-muted-foreground">
                    {t("settings.ai.advanced.compactionHint")}
                  </span>
                </span>
              </label>
              {draft.history_compaction && (
                <div className="grid gap-4 sm:grid-cols-2">
                  <label className="space-y-1.5 text-[13px]">
                    <span className="block font-medium text-foreground">
                      {t("settings.ai.advanced.threshold")}
                    </span>
                    <input
                      type="number"
                      required
                      min={50}
                      max={95}
                      step={1}
                      dir="ltr"
                      className={inputClass}
                      value={Math.round(draft.compaction_threshold * 100)}
                      onChange={(e) =>
                        update(
                          "compaction_threshold",
                          Number(e.target.value) / 100,
                        )
                      }
                    />
                    <span className="block text-xs leading-relaxed text-muted-foreground">
                      {t("settings.ai.advanced.thresholdHint")}
                    </span>
                  </label>
                  {numberField("recent_turns", 0, 32, "recent", "recentHint")}
                </div>
              )}
            </fieldset>
            <fieldset
              disabled={busy}
              className="grid gap-4 border-t border-border/50 pt-4 sm:grid-cols-2"
            >
              <label className="space-y-1.5 text-[13px]">
                <span className="block font-medium text-foreground">
                  {t("settings.ai.advanced.tools")}
                </span>
                <select
                  className={inputClass}
                  value={draft.tool_calling}
                  onChange={(e) =>
                    update(
                      "tool_calling",
                      e.target.value as AgentPreferences["tool_calling"],
                    )
                  }
                >
                  <option value="auto">{t("settings.ai.advanced.auto")}</option>
                  <option value="text">{t("settings.ai.advanced.text")}</option>
                </select>
                <span className="block text-xs leading-relaxed text-muted-foreground">
                  {t("settings.ai.advanced.toolsHint")}
                </span>
              </label>
              {numberField("repair_attempts", 0, 5, "repairs", "repairsHint")}
            </fieldset>
            <div className="flex flex-wrap items-center gap-2 border-t border-border/50 pt-4">
              <Button type="submit" size="sm" disabled={busy}>
                {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                {t("settings.ai.advanced.save")}
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={busy}
                onClick={() => void persist(true)}
              >
                {t("settings.ai.advanced.reset")}
              </Button>
              {saved && (
                <span role="status" className="text-xs text-success">
                  {t("settings.ai.advanced.saved")}
                </span>
              )}
            </div>
          </form>
        )}
        {error && (
          <div role="alert" className="text-xs text-danger">
            {t(`settings.ai.advanced.${error}`)}
            {error === "loadError" && (
              <Button
                variant="ghost"
                size="xs"
                onClick={() => setReload((n) => n + 1)}
              >
                {t("settings.ai.advanced.retry")}
              </Button>
            )}
          </div>
        )}
        <CoderAiSettings />
      </div>
    </details>
  );
}
