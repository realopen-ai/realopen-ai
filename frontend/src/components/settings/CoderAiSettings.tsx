import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { useT } from "@/store/settingsStore";
import { Button } from "@/components/ui/button";
import {
  fetchCoderSettings,
  saveCoderSettings,
  resetCoderSettings,
  validateCoderPreferences,
  type CoderPreferences,
  type CoderSettingsResponse,
} from "@/api/coderSettingsClient";

export function CoderAiSettings() {
  const t = useT();
  const [response, setResponse] = useState<CoderSettingsResponse | null>(null);
  const [draft, setDraft] = useState<CoderPreferences | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    fetchCoderSettings(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) {
          setResponse(data);
          setDraft(data.settings);
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) setError("loadError");
      });
    return () => controller.abort();
  }, [reload]);
  const persist = async (reset = false) => {
    if (!draft || !response || busy) return;
    const invalid = !reset && validateCoderPreferences(draft, response);
    if (invalid) {
      setError(invalid);
      return;
    }
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const data = reset
        ? await resetCoderSettings()
        : await saveCoderSettings(draft);
      setResponse(data);
      setDraft(data.settings);
      setSaved(true);
    } catch {
      setError("saveError");
    } finally {
      setBusy(false);
    }
  };
  const fields = [
    ["context_window", 2048, 524288, "context", "contextHint"],
    ["generation_limit", 128, 65536, "generation", "generationHint"],
    ["max_rounds", 4, 96, "rounds", "roundsHint"],
  ] as const;
  return (
    <section className="space-y-4 border-t border-border/50 pt-4">
      <h3 className="text-[13.5px] font-medium">
        {t("settings.ai.coder.title")}
      </h3>
      <p className="text-xs text-muted-foreground">
        {t("settings.ai.coder.description")}
      </p>
      {!draft && !error && (
        <Loader2
          className="h-4 w-4 animate-spin"
          aria-label={t("settings.ai.advanced.loading")}
        />
      )}
      {draft && (
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            void persist();
          }}
        >
          <fieldset disabled={busy} className="grid gap-4 sm:grid-cols-2">
            {fields.map(([key, min, max, label, hint]) => (
              <label key={key} className="space-y-1.5 text-[13px]">
                <span className="block font-medium">
                  {t(
                    `settings.ai.${key === "max_rounds" ? "coder" : "advanced"}.${label}`,
                  )}
                </span>
                <input
                  type="number"
                  required
                  min={min}
                  max={max}
                  step={1}
                  dir="ltr"
                  className="h-9 w-full rounded-lg border border-input bg-background px-3 text-[13px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                  value={draft[key]}
                  onChange={(e) => {
                    setDraft({ ...draft, [key]: Number(e.target.value) });
                    setError(null);
                    setSaved(false);
                  }}
                />
                <span className="block text-xs leading-relaxed text-muted-foreground">
                  {t(
                    `settings.ai.${key === "max_rounds" ? "coder" : "advanced"}.${hint}`,
                  )}
                </span>
              </label>
            ))}
          </fieldset>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" type="submit" disabled={busy}>
              {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              {t("settings.ai.advanced.save")}
            </Button>
            <Button
              size="sm"
              type="button"
              variant="ghost"
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
              size="xs"
              variant="ghost"
              onClick={() => setReload((n) => n + 1)}
            >
              {t("settings.ai.advanced.retry")}
            </Button>
          )}
        </div>
      )}
    </section>
  );
}
