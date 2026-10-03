import { useEffect, useMemo, useRef, useState } from "react";
import { Check, Loader2, Pencil, Plus, Trash2, Upload, X } from "lucide-react";

import {
  fetchVoiceSettings,
  fetchVoices,
  updateVoiceSettings,
  uploadVoice,
  type VoicePersona,
  type VoiceSettings,
} from "@/api/client";
import { ModelSelect } from "@/components/settings/ModelSelect";
import { useAiStore } from "@/store/aiStore";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { SectionHeader } from "@/components/ui/primitives";
import { SettingRow } from "@/components/brain/ToolsTab";
import { t, useT } from "@/store/settingsStore";
import {
  personaAfterDelete,
  removeCustomPersona,
  replaceCustomPersona,
} from "@/voice/personaControls";

const speedOptions = [0.5, 1, 1.5, 2];

const controlClass =
  "h-9 rounded-lg border border-border/60 bg-transparent px-3 text-[13px] text-foreground placeholder:text-muted-foreground/70 outline-none transition-colors focus:border-primary/50 focus:ring-2 focus:ring-primary/20";

export function VoiceTab() {
  useT();
  const [settings, setSettings] = useState<VoiceSettings | null>(null);
  const [voices, setVoices] = useState<{
    builtin: string[];
    custom: { id: string; name: string; voice: string }[];
  }>({ builtin: [], custom: [] });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [editingPersonaId, setEditingPersonaId] = useState<string | null>(null);
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const models = useAiStore((s) => s.models);
  const tasks = useAiStore((s) => s.tasks);
  const groqConnected = useAiStore((s) => s.groqConnected);
  const loadModels = useAiStore((s) => s.load);
  const selectModel = useAiStore((s) => s.selectModel);
  const model = tasks.find((task) => task.task === "voice") ?? settings?.model;

  useEffect(() => {
    void Promise.all([fetchVoiceSettings(), fetchVoices(), loadModels()])
      .then(([nextSettings, nextVoices]) => {
        setSettings(nextSettings);
        setVoices(nextVoices);
      })
      .catch((e) => setError(String(e)));
  }, [loadModels]);

  const save = async (patch: Partial<VoiceSettings>) => {
    if (!settings) return;
    const next = { ...settings, ...patch };
    setSettings(next);
    setSaving(true);
    setError("");
    try {
      await updateVoiceSettings(patch);
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  const personas = useMemo(
    () =>
      settings
        ? [
            ...Object.keys(settings.personas).map((id) => ({
              id,
              name: id[0].toUpperCase() + id.slice(1),
              prompt: settings.personas[id],
              custom: false,
            })),
            ...settings.custom_personas.map((persona) => ({
              ...persona,
              custom: true,
            })),
          ]
        : [],
    [settings],
  );

  const resetPersonaForm = () => {
    setEditingPersonaId(null);
    setName("");
    setPrompt("");
  };

  const beginPersonaEdit = (persona: VoicePersona) => {
    setDeleteConfirmId(null);
    setEditingPersonaId(persona.id);
    setName(persona.name);
    setPrompt(persona.prompt);
  };

  const savePersona = () => {
    if (!settings) return;
    const trimmedName = name.trim();
    const trimmedPrompt = prompt.trim();
    if (!trimmedName || !trimmedPrompt) return;
    if (editingPersonaId) {
      void save({
        custom_personas: replaceCustomPersona(settings.custom_personas, {
          id: editingPersonaId,
          name: trimmedName,
          prompt: trimmedPrompt,
        }),
      });
    } else {
      const persona: VoicePersona = {
        id: `custom-${Date.now()}`,
        name: trimmedName,
        prompt: trimmedPrompt,
      };
      void save({
        custom_personas: [...settings.custom_personas, persona],
        persona: persona.id,
      });
    }
    resetPersonaForm();
  };

  const deletePersona = (id: string) => {
    if (!settings) return;
    void save({
      custom_personas: removeCustomPersona(settings.custom_personas, id),
      persona: personaAfterDelete(settings.persona, id),
    });
    if (editingPersonaId === id) resetPersonaForm();
    setDeleteConfirmId(null);
  };

  if (!settings)
    return (
      <div className="flex gap-2 text-sm text-muted-foreground">
        <Loader2 className="w-4 h-4 animate-spin" />
        {t("settings.voice.loading")}
      </div>
    );

  return (
    <div className="space-y-7">
      {error && <p className="text-xs text-danger">{error}</p>}

      {/* ── Model / Voice / Speed rows ── */}
      <div className="divide-y divide-border/50">
        <SettingRow
          label={t("settings.voice.model")}
          help={t("settings.voice.modelHelp")}
        >
          {model && (
            <ModelSelect
              task="voice"
              models={models}
              currentModel={model.model}
              groqConnected={groqConnected}
              localOnly={false}
              onSelect={selectModel}
            />
          )}
        </SettingRow>

        <div className="py-3.5">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-4">
            <div className="min-w-0 flex-1">
              <div className="text-[13.5px] text-foreground">
                {t("settings.voice.voice")}
              </div>
            </div>
            <select
              value={settings.voice}
              onChange={(e) => void save({ voice: e.target.value })}
              className={cn(controlClass, "w-full sm:w-64 cursor-pointer")}
              aria-label={t("settings.voice.voice")}
            >
              <optgroup label={t("settings.voice.builtinVoices")}>
                {voices.builtin.map((voice) => (
                  <option key={voice} value={voice}>
                    {voice.replace(/_/g, " ")}
                  </option>
                ))}
              </optgroup>
              {voices.custom.length > 0 && (
                <optgroup label={t("settings.voice.myVoices")}>
                  {voices.custom.map((voice) => (
                    <option key={voice.id} value={voice.voice}>
                      {voice.name}
                    </option>
                  ))}
                </optgroup>
              )}
            </select>
          </div>

          <div className="mt-3 space-y-2.5">
            <input
              ref={fileRef}
              hidden
              type="file"
              accept=".wav,audio/wav"
              onChange={async (e) => {
                const file = e.target.files?.[0];
                if (!file) return;
                setSaving(true);
                setError("");
                try {
                  const added = await uploadVoice(file);
                  setVoices((v) => ({ ...v, custom: [...v.custom, added] }));
                  await save({ voice: added.voice });
                } catch (err) {
                  setError(String(err));
                } finally {
                  setSaving(false);
                  e.target.value = "";
                }
              }}
            />
            <div className="flex items-center gap-3">
              <Button
                variant="secondary"
                size="sm"
                onClick={() => fileRef.current?.click()}
                disabled={saving}
              >
                {saving ? <Loader2 className="animate-spin" /> : <Upload />}
                {t("settings.voice.cloneWav")}
              </Button>
              <p className="text-[11.5px] leading-relaxed text-muted-foreground">
                {t("settings.voice.cloneHelp")}
              </p>
            </div>
            <div className="space-y-1.5 rounded-lg bg-secondary/60 p-3 text-[11.5px] leading-relaxed text-muted-foreground">
              <p>
                {t("settings.voice.cloneTermsPrefix")}{" "}
                <a
                  href="https://huggingface.co/kyutai/pocket-tts"
                  target="_blank"
                  rel="noreferrer"
                  className="font-medium text-primary underline underline-offset-2"
                >
                  {t("settings.voice.cloneTermsLink")}
                </a>
                .
              </p>
              <p>
                {t("settings.voice.cloneLogin")}
                <code className="ml-1 rounded bg-secondary px-1.5 py-0.5 font-mono text-[11px] text-foreground">
                  uvx hf auth login
                </code>
              </p>
            </div>
          </div>
        </div>

        <SettingRow label={t("settings.voice.speed")}>
          <div
            className="inline-flex h-9 items-center rounded-lg bg-secondary p-1"
            role="group"
            aria-label={t("settings.voice.speed")}
          >
            {speedOptions.map((speed) => (
              <button
                key={speed}
                onClick={() => void save({ speed })}
                className={cn(
                  "h-7 rounded-md px-3 text-[12.5px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60",
                  settings.speed === speed
                    ? "bg-background text-foreground shadow-sm"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {speed}×
              </button>
            ))}
          </div>
        </SettingRow>
      </div>

      {/* ── Personas ── */}
      <section className="space-y-3">
        <SectionHeader
          title={t("settings.voice.persona")}
          description={t("settings.voice.personaHelp")}
        />
        <div className="grid grid-cols-2 gap-2">
          {personas.map((persona) => (
            <div
              key={persona.id}
              className={cn(
                "relative rounded-lg border transition-colors",
                settings.persona === persona.id
                  ? "border-primary/40 bg-primary/10"
                  : "border-border/60 hover:bg-surface-hover",
              )}
            >
              <button
                title={persona.prompt}
                onClick={() => void save({ persona: persona.id })}
                className={cn(
                  "w-full p-3 text-left text-[13px]",
                  persona.custom && "pr-16",
                  settings.persona === persona.id
                    ? "font-medium text-primary"
                    : "text-foreground",
                )}
              >
                {persona.name}
              </button>
              {persona.custom && (
                <div className="absolute right-1.5 top-1.5 flex items-center gap-0.5">
                  {deleteConfirmId === persona.id ? (
                    <>
                      <button
                        aria-label={`Confirm delete ${persona.name}`}
                        title={t("settings.voice.confirmDelete")}
                        onClick={() => deletePersona(persona.id)}
                        className="rounded-md p-1.5 text-danger transition-colors hover:bg-danger/10"
                      >
                        <Check className="w-3.5 h-3.5" />
                      </button>
                      <button
                        aria-label={`Cancel deleting ${persona.name}`}
                        title={t("workspace.cancel")}
                        onClick={() => setDeleteConfirmId(null)}
                        className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-surface-hover"
                      >
                        <X className="w-3.5 h-3.5" />
                      </button>
                    </>
                  ) : (
                    <>
                      <button
                        aria-label={`Edit ${persona.name}`}
                        title={t("settings.voice.editPersona")}
                        onClick={() => beginPersonaEdit(persona)}
                        className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-surface-hover hover:text-foreground"
                      >
                        <Pencil className="w-3.5 h-3.5" />
                      </button>
                      <button
                        aria-label={`Delete ${persona.name}`}
                        title={t("settings.voice.deletePersona")}
                        onClick={() => setDeleteConfirmId(persona.id)}
                        className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-danger/10 hover:text-danger"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>

        <div className="space-y-2.5 rounded-lg bg-secondary/50 p-3.5">
          <div className="flex items-center gap-2 text-[12.5px] font-medium text-foreground">
            {editingPersonaId ? (
              <Pencil className="w-3.5 h-3.5" />
            ) : (
              <Plus className="w-3.5 h-3.5" />
            )}
            {editingPersonaId
              ? t("settings.voice.editCustomPersona")
              : t("settings.voice.customPersona")}
          </div>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t("settings.voice.personaName")}
            className={cn(controlClass, "w-full")}
          />
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder={t("settings.voice.personaPrompt")}
            rows={3}
            className={cn(controlClass, "w-full resize-none py-2")}
          />
          <div className="flex gap-2">
            <Button
              size="sm"
              disabled={!name.trim() || !prompt.trim() || saving}
              onClick={savePersona}
            >
              {editingPersonaId
                ? t("settings.voice.saveChanges")
                : t("settings.voice.savePersona")}
            </Button>
            {editingPersonaId && (
              <Button variant="ghost" size="sm" onClick={resetPersonaForm}>
                {t("workspace.cancel")}
              </Button>
            )}
          </div>
        </div>
      </section>

      {saving && (
        <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
          <Loader2 className="w-3 h-3 animate-spin" />
          {t("settings.voice.saving")}
        </div>
      )}
    </div>
  );
}
