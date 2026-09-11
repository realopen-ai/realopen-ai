import { useEffect, useRef, useState } from "react";
import {
  Cloud,
  HardDrive,
  Loader2,
  Plus,
  RotateCcw,
  Sparkles,
  Eye,
  FileSearch,
  FileText,
  Sheet,
  Image as ImageIcon,
  MessageSquare,
} from "lucide-react";

import { useAiStore } from "@/store/aiStore";
import { useT, useSettingsStore } from "@/store/settingsStore";
import { ModelSelect } from "@/components/settings/ModelSelect";
import { cn } from "@/lib/utils";

// ─── Task icons ────────────────────────────────────────────────────

function TaskIcon({ task, className }: { task: string; className?: string }) {
  switch (task) {
    case "chat":
      return <MessageSquare className={className} />;
    case "vision":
      return <Eye className={className} />;
    case "document_reasoning":
      return <FileSearch className={className} />;
    case "report":
      return <FileText className={className} />;
    case "excel":
      return <Sheet className={className} />;
    case "image":
      return <ImageIcon className={className} />;
    default:
      return <Sparkles className={className} />;
  }
}

// ─── Providers section ─────────────────────────────────────────────

function ProviderDot({ connected }: { connected: boolean }) {
  return (
    <span
      className={cn(
        "shrink-0 w-2.5 h-2.5 rounded-full border-2 transition-colors",
        connected
          ? "bg-emerald-500 border-emerald-500"
          : "border-muted-foreground/40",
      )}
      aria-hidden
    />
  );
}

function ProvidersSection() {
  const t = useT();
  const language = useSettingsStore((s) => s.language);
  const providers = useAiStore((s) => s.providers);
  const isLoading = useAiStore((s) => s.isLoadingProviders);
  const isConnecting = useAiStore((s) => s.isConnectingGroq);
  const groqError = useAiStore((s) => s.groqError);
  const connectGroq = useAiStore((s) => s.connectGroq);
  const disconnectGroq = useAiStore((s) => s.disconnectGroq);

  const [apiKey, setApiKey] = useState("");
  const [showHint, setShowHint] = useState(false);
  const keyInputRef = useRef<HTMLInputElement>(null);

  const ollama = providers.find((p) => p.id === "ollama");
  const groq = providers.find((p) => p.id === "groq");

  const modelsLabel = (count: number) => {
    if (language === "fr") {
      return `${count} ${count === 1 ? "modèle disponible" : "modèles disponibles"}`;
    }
    return `${count} ${count === 1 ? "model available" : "models available"}`;
  };

  const handleConnect = async () => {
    if (!apiKey.trim() || isConnecting) return;
    const ok = await connectGroq(apiKey.trim());
    if (ok) setApiKey("");
  };

  return (
    <div className="space-y-5">
      {/* ── Local ── */}
      <div className="space-y-2">
        <div className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/60">
          {t("settings.ai.providers.local")}
        </div>
        <div className="flex items-center gap-3.5 p-3.5 rounded-xl border border-border bg-card">
          <ProviderDot connected={ollama?.connected ?? false} />
          <HardDrive className="shrink-0 w-4.5 h-4.5 text-muted-foreground" />
          <div className="flex-1 min-w-0">
            <div className="text-[13px] font-medium text-foreground">
              {ollama?.name ?? "Ollama"}
            </div>
            <div className="text-[12px] text-muted-foreground">
              {ollama?.connected
                ? `${t("settings.ai.providers.connected")} · ${modelsLabel(ollama.model_count ?? 0)}`
                : t("settings.ai.providers.disconnected")}
            </div>
          </div>
        </div>
      </div>

      {/* ── Cloud ── */}
      <div className="space-y-2">
        <div className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/60">
          {t("settings.ai.providers.cloud")}
        </div>
        <div className="p-3.5 rounded-xl border border-border bg-card space-y-3">
          <div className="flex items-center gap-3.5">
            <ProviderDot connected={groq?.connected ?? false} />
            <Cloud className="shrink-0 w-4.5 h-4.5 text-muted-foreground" />
            <div className="flex-1 min-w-0">
              <div className="text-[13px] font-medium text-foreground">
                {groq?.name ?? "Groq"}
              </div>
              <div className="text-[12px] text-muted-foreground truncate">
                {groq?.connected
                  ? `${t("settings.ai.providers.apiKey")}: ${groq.key_masked} · ${t("settings.ai.providers.connected")}`
                  : t("settings.ai.providers.disconnected")}
              </div>
            </div>

            {groq?.connected && (
              <button
                onClick={() => disconnectGroq()}
                className="shrink-0 px-2.5 py-1.5 rounded-lg text-[12px] text-muted-foreground hover:text-red-400 hover:bg-red-500/10 transition-colors"
              >
                {t("settings.ai.providers.disconnect")}
              </button>
            )}
          </div>

          {/* Connect form (only when not connected) */}
          {groq && !groq.connected && (
            <div className="space-y-2 pt-1">
              <div className="flex gap-2">
                <input
                  ref={keyInputRef}
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && handleConnect()}
                  placeholder={t("settings.ai.providers.apiKeyPlaceholder")}
                  className="flex-1 px-3 py-2 rounded-xl border border-border bg-card text-[13px] text-foreground placeholder:text-muted-foreground/50 outline-none focus:border-primary/40 focus:ring-1 focus:ring-primary/20"
                  autoComplete="off"
                  spellCheck={false}
                />
                <button
                  onClick={handleConnect}
                  disabled={!apiKey.trim() || isConnecting}
                  className={cn(
                    "shrink-0 flex items-center gap-1.5 px-3.5 py-2 rounded-xl text-[12px] font-medium transition-colors",
                    !apiKey.trim() || isConnecting
                      ? "bg-secondary text-muted-foreground cursor-not-allowed"
                      : "bg-primary text-primary-foreground hover:opacity-90",
                  )}
                >
                  {isConnecting ? (
                    <>
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                      {t("settings.ai.providers.connecting")}
                    </>
                  ) : (
                    t("settings.ai.providers.connect")
                  )}
                </button>
              </div>

              {groqError && (
                <p className="text-[12px] text-red-400">{groqError}</p>
              )}
              <p className="text-[11px] text-muted-foreground/70 leading-relaxed">
                {t("settings.ai.providers.connectHint")}
              </p>
            </div>
          )}
        </div>

        {/* + Add provider */}
        <button
          onClick={() => {
            if (groq?.connected) {
              setShowHint(true);
              setTimeout(() => setShowHint(false), 2600);
            } else {
              keyInputRef.current?.focus();
            }
          }}
          className="w-full flex items-center justify-center gap-1.5 px-3 py-2.5 rounded-xl border border-dashed border-border/70 text-[12px] text-muted-foreground hover:text-foreground hover:border-border transition-colors"
        >
          <Plus className="w-3.5 h-3.5" />
          {t("settings.ai.providers.addProvider")}
        </button>
        {showHint && (
          <p className="text-[11px] text-muted-foreground/70 text-center">
            {t("settings.ai.providers.noOther")}
          </p>
        )}
      </div>

      {isLoading && providers.length === 0 && (
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <Loader2 className="w-3.5 h-3.5 animate-spin" />
          {t("settings.ai.models.loading")}
        </div>
      )}
    </div>
  );
}

// ─── AI tab ────────────────────────────────────────────────────────

export function AiTab() {
  const t = useT();
  const models = useAiStore((s) => s.models);
  const tasks = useAiStore((s) => s.tasks);
  const groqConnected = useAiStore((s) => s.groqConnected);
  const isLoading = useAiStore((s) => s.isLoadingModels);
  const taskErrors = useAiStore((s) => s.taskErrors);
  const selectModel = useAiStore((s) => s.selectModel);
  const load = useAiStore((s) => s.load);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="space-y-7">
      <p className="text-[12px] text-muted-foreground leading-relaxed">
        {t("settings.ai.description")}
      </p>

      {/* ── Models ── */}
      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <span className="text-[13px] font-semibold text-foreground">
            {t("settings.ai.models")}
          </span>
          <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-secondary text-muted-foreground">
            {models.length}
          </span>
        </div>

        {isLoading && tasks.length === 0 && (
          <div className="flex items-center gap-2 text-[12px] text-muted-foreground py-4">
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
            {t("settings.ai.models.loading")}
          </div>
        )}

        <div className="rounded-xl border border-border divide-y divide-border/60">
          {tasks.map((slot) => {
            const label = t(`settings.ai.tasks.${slot.task}`);
            const error = taskErrors[slot.task];
            return (
              <div
                key={slot.task}
                className="flex flex-col sm:flex-row sm:items-center gap-2 sm:gap-3 p-3"
              >
                <div className="flex items-center gap-2.5 flex-1 min-w-0">
                  <TaskIcon
                    task={slot.task}
                    className="w-4 h-4 text-muted-foreground shrink-0"
                  />
                  <span className="text-[13px] text-foreground truncate">
                    {label}
                  </span>
                  {slot.is_default ? (
                    <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded-full bg-secondary text-muted-foreground/70">
                      {t("settings.ai.models.defaultSuffix")}
                    </span>
                  ) : (
                    <button
                      onClick={() => selectModel(slot.task, null)}
                      title={t("settings.ai.models.reset")}
                      className="shrink-0 flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full bg-primary/10 text-primary hover:bg-primary/20 transition-colors"
                    >
                      <RotateCcw className="w-2.5 h-2.5" />
                      {t("settings.ai.models.reset")}
                    </button>
                  )}
                  {error && (
                    <span className="text-[10px] text-red-400">
                      {t("settings.ai.models.saveError")}
                    </span>
                  )}
                </div>
                <ModelSelect
                  task={slot.task}
                  models={models}
                  currentModel={slot.model}
                  groqConnected={groqConnected}
                  localOnly={slot.local_only}
                  onSelect={selectModel}
                />
              </div>
            );
          })}
        </div>
      </div>

      {/* ── Providers ── */}
      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <span className="text-[13px] font-semibold text-foreground">
            {t("settings.ai.providers")}
          </span>
        </div>
        <ProvidersSection />
      </div>
    </div>
  );
}
