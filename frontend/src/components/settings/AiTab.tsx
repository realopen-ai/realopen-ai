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
  Bot,
} from "lucide-react";

import { useAiStore } from "@/store/aiStore";
import { useT, useSettingsStore } from "@/store/settingsStore";
import { ModelSelect } from "@/components/settings/ModelSelect";
import { Button } from "@/components/ui/button";
import { SectionHeader, StatusDot } from "@/components/ui/primitives";

// ─── Task icons ────────────────────────────────────────────────────

function TaskIcon({ task, className }: { task: string; className?: string }) {
  switch (task) {
    case "chat":
      return <Bot className={className} />;
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

  const groupLabel =
    "px-0.5 pb-1 text-[10.5px] font-medium uppercase tracking-wider text-muted-foreground/80";

  return (
    <div className="space-y-5">
      {/* ── Local ── */}
      <div>
        <div className={groupLabel}>{t("settings.ai.providers.local")}</div>
        <div className="divide-y divide-border/50">
          <div className="flex items-center gap-3 py-3">
            <StatusDot tone={ollama?.connected ? "success" : "neutral"} />
            <HardDrive className="h-4.5 w-4.5 shrink-0 text-muted-foreground" />
            <div className="min-w-0 flex-1">
              <div className="text-[13.5px] font-medium text-foreground">
                {ollama?.name ?? "Ollama"}
              </div>
              <div className="text-xs text-muted-foreground">
                {ollama?.connected
                  ? `${t("settings.ai.providers.connected")} · ${modelsLabel(ollama.model_count ?? 0)}`
                  : t("settings.ai.providers.disconnected")}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* ── Cloud ── */}
      <div>
        <div className={groupLabel}>{t("settings.ai.providers.cloud")}</div>
        <div className="divide-y divide-border/50">
          <div className="py-3">
            <div className="flex items-center gap-3">
              <StatusDot tone={groq?.connected ? "success" : "neutral"} />
              <Cloud className="h-4.5 w-4.5 shrink-0 text-muted-foreground" />
              <div className="min-w-0 flex-1">
                <div className="text-[13.5px] font-medium text-foreground">
                  {groq?.name ?? "Groq"}
                </div>
                <div className="truncate text-xs text-muted-foreground">
                  {groq?.connected
                    ? `${t("settings.ai.providers.apiKey")}: ${groq.key_masked} · ${t("settings.ai.providers.connected")}`
                    : t("settings.ai.providers.disconnected")}
                </div>
              </div>

              {groq?.connected && (
                <button
                  onClick={() => disconnectGroq()}
                  className="shrink-0 rounded-md px-2 py-1 text-[12px] text-muted-foreground transition-colors hover:bg-danger/10 hover:text-danger"
                >
                  {t("settings.ai.providers.disconnect")}
                </button>
              )}
            </div>

            {/* Connect form (only when not connected) */}
            {groq && !groq.connected && (
              <div className="mt-3 space-y-2">
                <div className="flex gap-2">
                  <input
                    ref={keyInputRef}
                    type="password"
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && handleConnect()}
                    placeholder={t("settings.ai.providers.apiKeyPlaceholder")}
                    className="h-9 flex-1 rounded-lg border border-border/60 bg-transparent px-3 text-[13px] text-foreground outline-none transition-colors placeholder:text-muted-foreground/70 focus:border-primary/50 focus:ring-2 focus:ring-primary/20"
                    autoComplete="off"
                    spellCheck={false}
                  />
                  <Button
                    size="sm"
                    onClick={handleConnect}
                    disabled={!apiKey.trim() || isConnecting}
                  >
                    {isConnecting ? (
                      <>
                        <Loader2 className="animate-spin" />
                        {t("settings.ai.providers.connecting")}
                      </>
                    ) : (
                      t("settings.ai.providers.connect")
                    )}
                  </Button>
                </div>

                {groqError && (
                  <p className="text-[12px] text-danger">{groqError}</p>
                )}
                <p className="text-[11.5px] leading-relaxed text-muted-foreground">
                  {t("settings.ai.providers.connectHint")}
                </p>
              </div>
            )}
          </div>
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
          className="mt-1 flex w-full items-center justify-center gap-1.5 rounded-lg border border-dashed border-border/60 px-3 py-2 text-[12.5px] text-muted-foreground transition-colors hover:border-border hover:text-foreground"
        >
          <Plus className="h-3.5 w-3.5" />
          {t("settings.ai.providers.addProvider")}
        </button>
        {showHint && (
          <p className="mt-1.5 text-center text-[11px] text-muted-foreground">
            {t("settings.ai.providers.noOther")}
          </p>
        )}
      </div>

      {isLoading && providers.length === 0 && (
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
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
      {/* ── Models ── */}
      <section>
        <SectionHeader
          title={t("settings.ai.models")}
          description={t("settings.ai.description")}
          actions={
            <span className="text-xs text-muted-foreground">
              {models.length}
            </span>
          }
        />

        {isLoading && tasks.length === 0 && (
          <div className="flex items-center gap-2 py-4 text-[12px] text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            {t("settings.ai.models.loading")}
          </div>
        )}

        <div className="divide-y divide-border/50">
          {tasks
            .filter((slot) => slot.task !== "voice")
            .map((slot) => {
              const label = t(`settings.ai.tasks.${slot.task}`);
              const error = taskErrors[slot.task];
              return (
                <div
                  key={slot.task}
                  className="flex flex-col gap-2 py-3 sm:flex-row sm:items-center sm:gap-4 sm:py-3.5"
                >
                  <div className="flex min-w-0 flex-1 items-center gap-2.5">
                    <TaskIcon
                      task={slot.task}
                      className="h-4 w-4 shrink-0 text-muted-foreground"
                    />
                    <span className="truncate text-[13.5px] text-foreground">
                      {label}
                    </span>
                    {slot.is_default ? (
                      <span className="shrink-0 rounded-full bg-secondary px-1.5 py-0.5 text-[11px] text-muted-foreground">
                        {t("settings.ai.models.defaultSuffix")}
                      </span>
                    ) : (
                      <button
                        onClick={() => selectModel(slot.task, null)}
                        title={t("settings.ai.models.reset")}
                        className="inline-flex shrink-0 items-center gap-1 text-[10.5px] text-muted-foreground transition-colors hover:text-foreground"
                      >
                        <RotateCcw className="h-3 w-3" />
                        {t("settings.ai.models.reset")}
                      </button>
                    )}
                    {error && (
                      <span className="shrink-0 text-[10.5px] text-danger">
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
      </section>

      {/* ── Providers ── */}
      <section>
        <SectionHeader title={t("settings.ai.providers")} />
        <ProvidersSection />
      </section>
    </div>
  );
}
