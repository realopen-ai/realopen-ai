import { useEffect, useMemo, useRef, useState } from "react";
import { Check, Loader2, Pencil, Plus, Trash2, Upload, X } from "lucide-react";

import {
  fetchVoiceSettings, fetchVoices, updateVoiceSettings, uploadVoice,
  type VoicePersona, type VoiceSettings,
} from "@/api/client";
import { ModelSelect } from "@/components/settings/ModelSelect";
import { useAiStore } from "@/store/aiStore";
import { cn } from "@/lib/utils";
import {
  personaAfterDelete,
  removeCustomPersona,
  replaceCustomPersona,
} from "@/voice/personaControls";

const speedOptions = [0.5, 1, 1.5, 2];

export function VoiceTab() {
  const [settings, setSettings] = useState<VoiceSettings | null>(null);
  const [voices, setVoices] = useState<{ builtin: string[]; custom: { id: string; name: string; voice: string }[] }>({ builtin: [], custom: [] });
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
      .then(([nextSettings, nextVoices]) => { setSettings(nextSettings); setVoices(nextVoices); })
      .catch((e) => setError(String(e)));
  }, [loadModels]);

  const save = async (patch: Partial<VoiceSettings>) => {
    if (!settings) return;
    const next = { ...settings, ...patch };
    setSettings(next);
    setSaving(true); setError("");
    try { await updateVoiceSettings(patch); } catch (e) { setError(String(e)); }
    finally { setSaving(false); }
  };

  const personas = useMemo(() => settings ? [
    ...Object.keys(settings.personas).map((id) => ({ id, name: id[0].toUpperCase() + id.slice(1), prompt: settings.personas[id], custom: false })),
    ...settings.custom_personas.map((persona) => ({ ...persona, custom: true })),
  ] : [], [settings]);

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

  if (!settings) return <div className="flex gap-2 text-sm text-muted-foreground"><Loader2 className="w-4 h-4 animate-spin" />Loading voice settings…</div>;

  return <div className="space-y-6">
    {error && <p className="text-xs text-red-400">{error}</p>}
    <section className="space-y-2">
      <h4 className="text-[13px] font-semibold">Model</h4>
      <p className="text-xs text-muted-foreground">A smaller model keeps spoken replies responsive. This does not change text chat.</p>
      {model && <ModelSelect task="voice" models={models} currentModel={model.model} groqConnected={groqConnected} localOnly={false} onSelect={selectModel} />}
    </section>

    <section className="space-y-2">
      <h4 className="text-[13px] font-semibold">Voice</h4>
      <select value={settings.voice} onChange={(e) => void save({ voice: e.target.value })} className="w-full rounded-xl border border-border bg-card px-3 py-2 text-sm">
        <optgroup label="Pocket TTS voices">{voices.builtin.map((voice) => <option key={voice} value={voice}>{voice.replace(/_/g, " ")}</option>)}</optgroup>
        {voices.custom.length > 0 && <optgroup label="My voices">{voices.custom.map((voice) => <option key={voice.id} value={voice.voice}>{voice.name}</option>)}</optgroup>}
      </select>
      <input ref={fileRef} hidden type="file" accept=".wav,audio/wav" onChange={async (e) => {
        const file = e.target.files?.[0]; if (!file) return;
        setSaving(true); setError("");
        try { const added = await uploadVoice(file); setVoices((v) => ({ ...v, custom: [...v.custom, added] })); await save({ voice: added.voice }); }
        catch (err) { setError(String(err)); } finally { setSaving(false); e.target.value = ""; }
      }} />
      <button onClick={() => fileRef.current?.click()} disabled={saving} className="inline-flex items-center gap-2 rounded-lg bg-secondary px-3 py-2 text-xs hover:bg-accent disabled:opacity-50">
        {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Upload className="w-3.5 h-3.5" />} Clone from WAV
      </button>
      <p className="text-[11px] text-muted-foreground">Use a clean, single-speaker recording. The voice state is precomputed for fast reuse.</p>
      <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 p-3 text-[11px] text-muted-foreground space-y-1.5">
        <p>
          Voice cloning requires access to Kyutai&apos;s gated model. First{" "}
          <a
            href="https://huggingface.co/kyutai/pocket-tts"
            target="_blank"
            rel="noreferrer"
            className="font-medium text-primary underline underline-offset-2"
          >
            accept the Pocket TTS terms on Hugging Face
          </a>
          .
        </p>
        <p>
          Then authenticate on the host and restart the app:
          <code className="ml-1 rounded bg-secondary px-1.5 py-0.5 text-foreground">uvx hf auth login</code>
        </p>
      </div>
    </section>

    <section className="space-y-2">
      <h4 className="text-[13px] font-semibold">Speed</h4>
      <div className="flex gap-2">{speedOptions.map((speed) => <button key={speed} onClick={() => void save({ speed })} className={cn("flex-1 rounded-lg border px-2 py-2 text-xs", settings.speed === speed ? "border-primary bg-primary/10 text-primary" : "border-border")}>{speed}×</button>)}</div>
    </section>

    <section className="space-y-3">
      <h4 className="text-[13px] font-semibold">Persona</h4>
      <div className="grid grid-cols-2 gap-2">{personas.map((persona) => <div key={persona.id} className={cn("relative rounded-xl border", settings.persona === persona.id ? "border-primary bg-primary/10" : "border-border")}>
        <button title={persona.prompt} onClick={() => void save({ persona: persona.id })} className={cn("w-full p-3 text-left text-xs", persona.custom && "pr-16")}>{persona.name}</button>
        {persona.custom && <div className="absolute right-1.5 top-1.5 flex items-center gap-0.5">
          {deleteConfirmId === persona.id ? <>
            <button aria-label={`Confirm delete ${persona.name}`} title="Confirm delete" onClick={() => deletePersona(persona.id)} className="rounded-md p-1.5 text-red-400 hover:bg-red-500/10"><Check className="w-3.5 h-3.5" /></button>
            <button aria-label={`Cancel deleting ${persona.name}`} title="Cancel" onClick={() => setDeleteConfirmId(null)} className="rounded-md p-1.5 text-muted-foreground hover:bg-accent"><X className="w-3.5 h-3.5" /></button>
          </> : <>
            <button aria-label={`Edit ${persona.name}`} title="Edit persona" onClick={() => beginPersonaEdit(persona)} className="rounded-md p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"><Pencil className="w-3.5 h-3.5" /></button>
            <button aria-label={`Delete ${persona.name}`} title="Delete persona" onClick={() => setDeleteConfirmId(persona.id)} className="rounded-md p-1.5 text-muted-foreground hover:bg-red-500/10 hover:text-red-400"><Trash2 className="w-3.5 h-3.5" /></button>
          </>}
        </div>}
      </div>)}</div>
      <div className="rounded-xl border border-border p-3 space-y-2">
        <div className="flex items-center gap-2 text-xs font-medium">{editingPersonaId ? <Pencil className="w-3.5 h-3.5" /> : <Plus className="w-3.5 h-3.5" />}{editingPersonaId ? "Edit custom persona" : "Custom persona"}</div>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name" className="w-full rounded-lg border border-border bg-background px-3 py-2 text-xs" />
        <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="Describe how the assistant should speak…" rows={3} className="w-full resize-none rounded-lg border border-border bg-background px-3 py-2 text-xs" />
        <div className="flex gap-2">
          <button disabled={!name.trim() || !prompt.trim() || saving} onClick={savePersona} className="rounded-lg bg-primary px-3 py-2 text-xs text-primary-foreground disabled:opacity-40">{editingPersonaId ? "Save changes" : "Save persona"}</button>
          {editingPersonaId && <button onClick={resetPersonaForm} className="rounded-lg bg-secondary px-3 py-2 text-xs hover:bg-accent">Cancel</button>}
        </div>
      </div>
    </section>
    {saving && <div className="flex items-center gap-2 text-[11px] text-muted-foreground"><Loader2 className="w-3 h-3 animate-spin" />Saving…</div>}
  </div>;
}
