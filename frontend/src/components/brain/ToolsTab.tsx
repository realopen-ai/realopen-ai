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
 *                - use_image_gen → dedicated ImageGenCustom component
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
  ChevronRight,
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
import { useToolsStore } from "@/store/toolsStore";
import { useAiStore } from "@/store/aiStore";
import { useT } from "@/store/settingsStore";
import { cn } from "@/lib/utils";
import type { ToolInfo } from "@/api/toolsClient";
import { ModelSelect } from "@/components/settings/ModelSelect";
import { Button } from "@/components/ui/button";
import {
  EmptyState,
  SectionHeader,
  StatusDot,
} from "@/components/ui/primitives";
import { WebSearchCustom } from "@/components/brain/tools/WebSearchCustom";
import { ImageGenCustom } from "@/components/brain/tools/ImageGenCustom";
import { GenericCustomConfig } from "@/components/brain/tools/GenericCustomConfig";

// ─── Tool icons by name (cosmetic; unknown tools fall back to Wrench) ─

function ToolIcon({
  toolName,
  className,
}: {
  toolName: string;
  className?: string;
}) {
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
        "relative h-5.5 w-10 shrink-0 rounded-full transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60",
        checked ? "bg-primary" : "bg-secondary hover:bg-surface-hover",
        disabled && "cursor-not-allowed opacity-50",
      )}
    >
      <span
        className={cn(
          "absolute left-0.5 top-0.5 h-4.5 w-4.5 rounded-full bg-background shadow-sm transition-transform",
          checked && "translate-x-4.5",
        )}
      />
    </button>
  );
}

// ─── Row layout for a universal setting ─────────────────────────────

export function SettingRow({
  label,
  help,
  children,
  className,
}: {
  label: string;
  help?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex items-center gap-4 py-3.5", className)}>
      <div className="min-w-0 flex-1">
        <div className="text-[13.5px] text-foreground">{label}</div>
        {help && (
          <div className="mt-0.5 text-xs leading-relaxed text-muted-foreground/80">
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
    <div
      className={cn(
        "flex min-h-9 w-full max-w-md cursor-text flex-wrap items-center gap-1.5 rounded-lg border border-border/60 bg-transparent px-2 py-1.5",
        "transition-colors focus-within:border-primary/50 focus-within:ring-2 focus-within:ring-primary/20",
        disabled && "opacity-50",
      )}
      onClick={() => inputRef.current?.focus()}
    >
      {tags.map((tag) => (
        <span
          key={tag}
          className="inline-flex h-6 items-center gap-1 rounded-md bg-primary/10 pl-2 pr-1 text-[11.5px] font-medium text-primary"
        >
          <span className="max-w-45 truncate">{tag}</span>
          <button
            aria-label={`${t("brain.tools.removeTag")}: ${tag}`}
            disabled={disabled}
            onClick={(e) => {
              e.stopPropagation();
              remove(tag);
            }}
            className="flex h-4 w-4 shrink-0 items-center justify-center rounded transition-colors hover:bg-primary/20"
          >
            <X className="h-3 w-3" />
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
        className="min-w-27.5 flex-1 bg-transparent text-[12.5px] text-foreground outline-none placeholder:text-muted-foreground/70 disabled:opacity-50"
      />
      {input.trim() && (
        <button
          onClick={() => add(input)}
          className="flex h-5 w-5 shrink-0 items-center justify-center rounded text-muted-foreground transition-colors hover:text-primary"
          aria-label={t("brain.tools.addTag")}
        >
          <Plus className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  );
}

// ─── Tool list row ─────────────────────────────────────────────────

function ToolRow({ tool, onOpen }: { tool: ToolInfo; onOpen: () => void }) {
  return (
    <button
      onClick={onOpen}
      className={cn(
        "group flex w-full items-center gap-3.5 rounded-lg px-3 py-3 text-left transition-colors hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60",
        !tool.config.enabled && "opacity-60",
      )}
    >
      <span
        className={cn(
          "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg",
          tool.config.enabled
            ? "bg-primary/10 text-primary"
            : "bg-secondary text-muted-foreground/80",
        )}
      >
        <ToolIcon toolName={tool.tool} className="h-4.5 w-4.5" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-2.5">
          <span className="truncate text-[13.5px] font-medium text-foreground">
            {tool.display_name}
          </span>
          <StatusDot
            tone={tool.config.enabled ? "success" : "neutral"}
            label={tool.config.enabled ? "Enabled" : "Disabled"}
          />
        </span>
        <span className="mt-0.5 block truncate text-xs text-muted-foreground">
          {tool.description}
        </span>
      </span>
      <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground/70 transition-colors group-hover:text-foreground" />
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
    <div className="mx-auto w-full max-w-190">
      {/* Header */}
      <div className="mb-7">
        <Button variant="ghost" size="sm" className="-ml-2" onClick={onBack}>
          <ChevronLeft />
          {t("brain.tools.back")}
        </Button>
        <div className="mt-2 flex items-start gap-3.5">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-primary/10 text-primary">
            <ToolIcon toolName={tool.tool} className="h-5 w-5" />
          </span>
          <div className="min-w-0 flex-1">
            <h2 className="text-[18px] font-semibold tracking-[-0.01em] text-foreground">
              {tool.display_name}
            </h2>
            <p className="mt-0.5 text-[13px] leading-relaxed text-muted-foreground">
              {tool.description}
            </p>
          </div>
          {isSaving && (
            <Loader2 className="mt-1.5 h-4 w-4 shrink-0 animate-spin text-muted-foreground" />
          )}
        </div>
      </div>

      {/* Body */}
      <div className="space-y-9">
        {saveError && (
          <div className="rounded-lg bg-danger/10 px-3 py-2.5 text-[12.5px] text-danger">
            {saveError}
          </div>
        )}

        {/* ── General ── */}
        <section>
          <SectionHeader title={t("brain.tools.general")} />
          <div className="divide-y divide-border/50">
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
            <div className="py-3.5">
              <div className="mb-2.5">
                <div className="text-[13.5px] text-foreground">
                  {t("brain.tools.tags")}
                </div>
                <div className="mt-0.5 text-xs leading-relaxed text-muted-foreground/80">
                  {t("brain.tools.tagsHelp")}
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
                      tool.effective_model ? ` (${tool.effective_model})` : ""
                    }`
              }
            >
              <ModelSelect
                task={tool.tool}
                models={models}
                currentModel={cfg.model}
                groqConnected={groqConnected}
                localOnly={
                  tool.tool === "use_image_gen" || tool.tool === "use_vision"
                }
                inheritOption={t("brain.tools.modelInherit")}
                onSelect={(_task, model) => updateConfig(tool.tool, { model })}
              />
            </SettingRow>
          </div>
        </section>

        {/* ── Custom settings (each renderer draws its own sections) ── */}
        {tool.has_custom &&
          (tool.tool === "use_websearch" ? (
            <WebSearchCustom tool={tool} />
          ) : tool.tool === "use_image_gen" ? (
            <ImageGenCustom tool={tool} />
          ) : (
            <GenericCustomConfig tool={tool} />
          ))}

        {/* ── Coming soon hint for tools without custom config ── */}
        {!tool.has_custom && (
          <p className="text-[12.5px] leading-relaxed text-muted-foreground/70">
            {t("brain.tools.noCustomYet")}
          </p>
        )}
      </div>
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

  if (selected) {
    return <ToolDetail tool={selected} onBack={() => setSelectedTool(null)} />;
  }

  return (
    <div className="flex flex-col">
      {/* Intro */}
      <p className="mb-4 max-w-2xl text-[13px] leading-relaxed text-muted-foreground">
        {t("brain.tools.description")}
      </p>

      {/* Tool rows */}
      {isLoading && tools.length === 0 && (
        <div className="flex items-center justify-center gap-2 py-12 text-[12.5px] text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          {t("brain.tools.loading")}
        </div>
      )}

      <div className="divide-y divide-border/50">
        {tools.map((tool) => (
          <ToolRow
            key={tool.tool}
            tool={tool}
            onOpen={() => setSelectedTool(tool.tool)}
          />
        ))}
      </div>

      {!isLoading && tools.length === 0 && (
        <EmptyState icon={<Wrench />} title={t("brain.tools.empty")} />
      )}
    </div>
  );
}
