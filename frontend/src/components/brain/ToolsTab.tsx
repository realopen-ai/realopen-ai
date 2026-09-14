/**
 * Brain ▸ Tools tab — the dynamic tool configuration UI.
 *
 * Two views:
 *  1. List   — every tool discovered by the backend, with its
 *              enabled state obvious at a glance.
 *  2. Detail — the universal settings (Enabled / Always load /
 *              Tags / Model override) rendered generically, then the
 *              tool's CUSTOM settings:
 *                - use_websearch → dedicated WebSearchCustom component
 *                - others        → generic renderer driven by the
 *                                  backend's custom_schema
 *                - none          → basic settings only
 *
 * The list is fully dynamic (GET /api/tools) — adding a backend tool
 * makes it appear here without any frontend change.
 */

import { useEffect, useRef, useState } from "react";
import {
  Brain,
  ChevronLeft,
  Eye,
  FileSearch,
  FileText,
  Image as ImageIcon,
  Loader2,
  MessageSquare,
  Plus,
  Search,
  Sheet,
  Terminal,
  Wrench,
  X,
  Zap,
} from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useToolsStore } from "@/store/toolsStore";
import { useAiStore } from "@/store/aiStore";
import { useT } from "@/store/settingsStore";
import { cn } from "@/lib/utils";
import type { ToolInfo } from "@/api/toolsClient";
import { ModelSelect } from "@/components/settings/ModelSelect";
import { WebSearchCustom } from "@/components/brain/tools/WebSearchCustom";
import { GenericCustomConfig } from "@/components/brain/tools/GenericCustomConfig";

// ─── Tool icons by name (cosmetic; unknown tools fall back to Wrench) ─

function ToolIcon({ toolName, className }: { toolName: string; className?: string }) {
  switch (toolName) {
    case "use_websearch":
      return <Search className={className} />;
    case "use_vision":
      return <Eye className={className} />;
    case "use_code_exec":
      return <Terminal className={className} />;
    case "use_webfetch":
      return <Zap className={className} />;
    case "use_image_gen":
      return <ImageIcon className={className} />;
    case "rag_search":
      return <FileSearch className={className} />;
    case "manage_memory":
      return <Brain className={className} />;
    case "search_past_conversations":
      return <MessageSquare className={className} />;
    case "use_report_gen":
      return <FileText className={className} />;
    case "use_pptx_gen":
    case "use_excel_gen":
      return <Sheet className={className} />;
    default:
      return <Wrench className={className} />;
  }
}

// ─── Toggle switch (matches the existing design language) ──────────

export function SettingToggle({
  checked,
  onChange,
  disabled,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  label: string;
}) {
  return (
    <button
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        "relative shrink-0 w-10 h-6 rounded-full transition-colors",
        checked ? "bg-primary" : "bg-secondary",
        disabled && "opacity-50 cursor-not-allowed",
      )}
    >
      <span
        className={cn(
          "absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-background shadow transition-transform",
          checked && "translate-x-4",
        )}
      />
    </button>
  );
}

// ─── Row layout for a universal setting ─────────────────────────────

function SettingRow({
  label,
  help,
  children,
}: {
  label: string;
  help?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center gap-3 py-2.5">
      <div className="flex-1 min-w-0">
        <div className="text-[13px] text-foreground">{label}</div>
        {help && (
          <div className="text-[11px] text-muted-foreground/70 leading-snug mt-0.5">
            {help}
          </div>
        )}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

// ─── Tags editor (chips; each add/remove saves immediately) ────────

function TagsField({
  tool,
  tags,
  disabled,
}: {
  tool: string;
  tags: string[];
  disabled: boolean;
}) {
  const t = useT();
  const updateConfig = useToolsStore((s) => s.updateConfig);
  const [input, setInput] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  const add = (raw: string) => {
    const value = raw.trim().replace(/,+$/, "").trim();
    if (!value) {
      setInput("");
      return;
    }
    const exists = tags.some(
      (tag) => tag.toLowerCase() === value.toLowerCase(),
    );
    if (!exists) {
      updateConfig(tool, { tags: [...tags, value] });
    }
    setInput("");
  };

  const remove = (tag: string) => {
    updateConfig(tool, { tags: tags.filter((x) => x !== tag) });
  };

  return (
    <div className="flex flex-wrap items-center gap-1.5 rounded-xl border border-border bg-card px-2 py-1.5 min-h-[40px] cursor-text disabled:opacity-50"
      onClick={() => inputRef.current?.focus()}
    >
      {tags.map((tag) => (
        <span
          key={tag}
          className="inline-flex items-center gap-1 pl-2.5 pr-1 py-1 rounded-lg bg-primary/10 text-primary text-[11.5px] font-medium max-w-[180px]"
        >
          <span className="truncate">{tag}</span>
          <button
            aria-label={`${t("brain.tools.removeTag")}: ${tag}`}
            disabled={disabled}
            onClick={(e) => {
              e.stopPropagation();
              remove(tag);
            }}
            className="shrink-0 w-4 h-4 rounded flex items-center justify-center hover:bg-primary/20 transition-colors"
          >
            <X className="w-3 h-3" />
          </button>
        </span>
      ))}
      <input
        ref={inputRef}
        type="text"
        value={input}
        disabled={disabled}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === ",") {
            e.preventDefault();
            add(input);
          } else if (e.key === "Backspace" && !input && tags.length) {
            e.preventDefault();
            remove(tags[tags.length - 1]);
          }
        }}
        onBlur={() => input && add(input)}
        placeholder={
          tags.length === 0
            ? t("brain.tools.tagsPlaceholder")
            : t("brain.tools.tagsAddMore")
        }
        className="flex-1 min-w-[110px] bg-transparent text-[12.5px] text-foreground placeholder:text-muted-foreground/50 outline-none disabled:opacity-50"
      />
      {input.trim() && (
        <button
          onClick={() => add(input)}
          className="shrink-0 w-5 h-5 rounded flex items-center justify-center text-muted-foreground hover:text-primary transition-colors"
          aria-label={t("brain.tools.addTag")}
        >
          <Plus className="w-3.5 h-3.5" />
        </button>
      )}
    </div>
  );
}

// ─── Tool list row ─────────────────────────────────────────────────

function ToolCard({
  tool,
  onOpen,
}: {
  tool: ToolInfo;
  onOpen: () => void;
}) {
  return (
    <button
      onClick={onOpen}
      className={cn(
        "w-full flex items-center gap-3.5 px-4 py-3.5 text-left transition-colors hover:bg-accent/40 border-b border-border/40 last:border-b-0",
        !tool.config.enabled && "opacity-60",
      )}
    >
      <span
        className={cn(
          "shrink-0 w-9 h-9 rounded-xl flex items-center justify-center",
          tool.config.enabled
            ? "bg-primary/10 text-primary"
            : "bg-secondary text-muted-foreground/60",
        )}
      >
        <ToolIcon toolName={tool.tool} className="w-4.5 h-4.5" />
      </span>
      <span className="flex-1 min-w-0">
        <span className="flex items-center gap-2">
          <span className="text-[13.5px] font-medium text-foreground truncate">
            {tool.display_name}
          </span>
          <span
            className={cn(
              "shrink-0 inline-flex items-center gap-1 text-[10.5px] px-1.5 py-0.5 rounded-full",
              tool.config.enabled
                ? "bg-emerald-500/10 text-emerald-500"
                : "bg-secondary text-muted-foreground/70",
            )}
          >
            <span
              className={cn(
                "w-1.5 h-1.5 rounded-full",
                tool.config.enabled ? "bg-emerald-500" : "bg-muted-foreground/50",
              )}
            />
            {tool.config.enabled ? "Enabled" : "Disabled"}
          </span>
        </span>
        <span className="block text-[12px] text-muted-foreground/80 truncate mt-0.5">
          {tool.description}
        </span>
      </span>
      <ChevronLeft className="shrink-0 w-4 h-4 text-muted-foreground/40 rotate-180" />
    </button>
  );
}

// ─── Detail view ───────────────────────────────────────────────────

function ToolDetail({ tool, onBack }: { tool: ToolInfo; onBack: () => void }) {
  const t = useT();
  const updateConfig = useToolsStore((s) => s.updateConfig);
  const saveError = useToolsStore((s) => s.saveErrors[tool.tool] ?? null);
  const isSaving = useToolsStore((s) => !!s.saving[tool.tool]);
  const models = useAiStore((s) => s.models);
  const groqConnected = useAiStore((s) => s.groqConnected);
  const loadModels = useAiStore((s) => s.load);

  useEffect(() => {
    loadModels();
  }, [loadModels]);

  const cfg = tool.config;

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* Header */}
      <div className="flex items-center gap-2.5 px-4 py-3 border-b border-border/50">
        <button
          onClick={onBack}
          className="shrink-0 flex items-center gap-1 text-[12.5px] text-muted-foreground hover:text-foreground transition-colors"
        >
          <ChevronLeft className="w-4 h-4" />
          {t("brain.tools.back")}
        </button>
        <div className="flex-1 min-w-0 flex items-center gap-2.5 pl-1">
          <span className="shrink-0 w-8 h-8 rounded-xl bg-primary/10 text-primary flex items-center justify-center">
            <ToolIcon toolName={tool.tool} className="w-4 h-4" />
          </span>
          <div className="min-w-0">
            <div className="text-[14px] font-semibold text-foreground truncate">
              {tool.display_name}
            </div>
            <div className="text-[11.5px] text-muted-foreground/80 truncate">
              {tool.description}
            </div>
          </div>
        </div>
        {isSaving && (
          <Loader2 className="shrink-0 w-4 h-4 animate-spin text-muted-foreground" />
        )}
      </div>

      {/* Body */}
      <ScrollArea className="flex-1 min-h-0">
        <div className="max-w-2xl mx-auto px-4 py-4 space-y-6">
          {saveError && (
            <div className="px-3 py-2.5 rounded-xl border border-red-500/30 bg-red-500/10 text-[12px] text-red-400">
              {saveError}
            </div>
          )}

          {/* ── General ── */}
          <section className="space-y-1">
            <div className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/60 mb-1">
              {t("brain.tools.general")}
            </div>
            <div className="rounded-xl border border-border bg-card px-4 divide-y divide-border/50">
              <SettingRow
                label={t("brain.tools.enabled")}
                help={t("brain.tools.enabledHelp")}
              >
                <SettingToggle
                  checked={cfg.enabled}
                  label={t("brain.tools.enabled")}
                  onChange={(v) => updateConfig(tool.tool, { enabled: v })}
                />
              </SettingRow>
              <SettingRow
                label={t("brain.tools.alwaysLoad")}
                help={t("brain.tools.alwaysLoadHelp")}
              >
                <SettingToggle
                  checked={cfg.always_load}
                  disabled={!cfg.enabled}
                  label={t("brain.tools.alwaysLoad")}
                  onChange={(v) =>
                    updateConfig(tool.tool, {
                      always_load: v,
                      // Re-arm the default tags when enabling
                      // always-load with an empty tag list (so turning
                      // it off later keeps a working gate)
                      ...(v && !(cfg.tags?.length ?? 0)
                        ? { tags: tool.default_tags ?? [] }
                        : {}),
                    })
                  }
                />
              </SettingRow>
              <div className="py-2.5">
                <div className="flex items-baseline justify-between gap-3 mb-2">
                  <div className="min-w-0">
                    <div className="text-[13px] text-foreground">
                      {t("brain.tools.tags")}
                    </div>
                    <div className="text-[11px] text-muted-foreground/70 leading-snug mt-0.5">
                      {t("brain.tools.tagsHelp")}
                    </div>
                  </div>
                </div>
                <TagsField
                  tool={tool.tool}
                  tags={cfg.tags ?? []}
                  disabled={cfg.always_load || !cfg.enabled}
                />
              </div>
              <SettingRow
                label={t("brain.tools.model")}
                help={
                  cfg.model
                    ? undefined
                    : `${t("brain.tools.modelHelp")}${
                        tool.effective_model
                          ? ` (${tool.effective_model})`
                          : ""
                      }`
                }
              >
                <ModelSelect
                  task={tool.tool}
                  models={models}
                  currentModel={cfg.model}
                  groqConnected={groqConnected}
                  localOnly={tool.tool === "use_image_gen" || tool.tool === "use_vision"}
                  inheritOption={t("brain.tools.modelInherit")}
                  onSelect={(_task, model) =>
                    updateConfig(tool.tool, { model })
                  }
                />
              </SettingRow>
            </div>
          </section>

          {/* ── Custom settings ── */}
          {tool.has_custom && (
            <section className="space-y-1">
              <div className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/60 mb-1">
                {t("brain.tools.customSettings")}
              </div>
              {tool.tool === "use_websearch" ? (
                <WebSearchCustom tool={tool} />
              ) : (
                <GenericCustomConfig tool={tool} />
              )}
            </section>
          )}

          {/* ── Coming soon hint for tools without custom config ── */}
          {!tool.has_custom && (
            <p className="text-[11.5px] text-muted-foreground/60 leading-relaxed">
              {t("brain.tools.noCustomYet")}
            </p>
          )}
        </div>
      </ScrollArea>
    </div>
  );
}

// ─── Tab root ──────────────────────────────────────────────────────

export function ToolsTab() {
  const t = useT();
  const tools = useToolsStore((s) => s.tools);
  const isLoading = useToolsStore((s) => s.isLoading);
  const load = useToolsStore((s) => s.load);
  const [selectedTool, setSelectedTool] = useState<string | null>(null);

  useEffect(() => {
    load();
  }, [load]);

  const selected = tools.find((x) => x.tool === selectedTool) ?? null;

  return (
    <div className="h-full min-h-0 flex flex-col">
      {selected ? (
        <ToolDetail tool={selected} onBack={() => setSelectedTool(null)} />
      ) : (
        <div className="flex flex-col h-full min-h-0">
          <div className="px-5 pt-4 pb-2">
            <p className="text-[12px] text-muted-foreground leading-relaxed">
              {t("brain.tools.description")}
            </p>
          </div>
          <ScrollArea className="flex-1 min-h-0">
            <div className="max-w-2xl mx-auto w-full px-4 pb-4">
              {isLoading && tools.length === 0 && (
                <div className="flex items-center gap-2 text-[12px] text-muted-foreground py-8 justify-center">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  {t("brain.tools.loading")}
                </div>
              )}
              <div className="rounded-xl border border-border bg-card overflow-hidden">
                {tools.map((tool) => (
                  <ToolCard
                    key={tool.tool}
                    tool={tool}
                    onOpen={() => setSelectedTool(tool.tool)}
                  />
                ))}
              </div>
              {!isLoading && tools.length === 0 && (
                <div className="flex flex-col items-center justify-center py-16 text-center">
                  <Wrench className="w-10 h-10 text-muted-foreground/20 mb-3" />
                  <p className="text-[13px] font-medium text-muted-foreground/60">
                    {t("brain.tools.empty")}
                  </p>
                </div>
              )}
            </div>
          </ScrollArea>
        </div>
      )}
    </div>
  );
}
