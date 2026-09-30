import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Search, Sparkles } from "lucide-react";

import type { AvailableModel } from "@/api/client";
import { useT } from "@/store/settingsStore";
import { cn } from "@/lib/utils";

/**
 * Searchable model picker for the Settings ▸ AI ▸ Models section and
 * the Brain ▸ Tools model override.
 *
 * - Lists every model available to pick from.
 * - Search filters on the display name and the raw model id.
 * - `localOnly` slots (vision / image) show cloud models greyed out
 *   with an explanatory hint — the backend rejects them anyway.
 * - `inheritOption` (Tools tab) adds a first row meaning "no override —
 *   use the general model"; selected when currentModel is null/"".
 */

/** Status dot: green = pulled locally, amber = cloud, grey = not pulled. */
function modelDotClass(model: AvailableModel) {
  if (model.provider === "groq") return "bg-warning";
  if (model.installed === false) return "bg-muted-foreground/40";
  return "bg-success";
}

function ModelRow({
  model,
  selected,
  disabled,
  reason,
  onSelect,
}: {
  model: AvailableModel;
  selected: boolean;
  disabled: boolean;
  reason?: string;
  onSelect: (id: string) => void;
}) {
  const t = useT();
  return (
    <button
      onClick={() => !disabled && onSelect(model.id)}
      disabled={disabled}
      className={cn(
        "flex w-full items-center gap-2.5 px-3 py-2 text-left transition-colors",
        selected ? "bg-surface-selected" : "hover:bg-surface-hover",
        disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer",
      )}
    >
      {/* Status dot */}
      <span
        className={cn(
          "h-1.75 w-1.75 shrink-0 rounded-full",
          modelDotClass(model),
        )}
        aria-hidden
      />

      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] text-foreground">
          {model.description}
        </span>
        <span className="block truncate text-[11px] text-muted-foreground">
          {model.id}
          {model.provider === "ollama" &&
            !model.installed &&
            ` · ${t("settings.ai.models.notInstalled")}`}
          {model.provider === "groq" && " · cloud"}
        </span>
      </span>

      {disabled && reason ? (
        <span className="shrink-0 text-[10.5px] text-muted-foreground">
          {reason}
        </span>
      ) : selected ? (
        <Check className="h-3.5 w-3.5 shrink-0 text-primary" />
      ) : null}
    </button>
  );
}

export function ModelSelect({
  task,
  models,
  currentModel,
  groqConnected,
  localOnly,
  onSelect,
  inheritOption,
}: {
  task: string;
  models: AvailableModel[];
  currentModel: string | null;
  groqConnected: boolean;
  localOnly: boolean;
  onSelect: (task: string, model: string | null) => void;
  /** Label for the "inherit the default" row (omit = no inherit row). */
  inheritOption?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  const isInherit = inheritOption !== undefined && !currentModel;
  const current = models.find((m) => m.id === currentModel);
  const t = useT();

  // Close on outside click / Escape
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  // Focus the search field whenever the dropdown opens
  useEffect(() => {
    if (open) {
      setQuery("");
      requestAnimationFrame(() => searchRef.current?.focus());
    }
  }, [open]);

  const { localModels, cloudModels } = useMemo(() => {
    const q = query.trim().toLowerCase();
    const matches = (m: AvailableModel) =>
      !q ||
      m.description.toLowerCase().includes(q) ||
      m.id.toLowerCase().includes(q);
    return {
      localModels: models.filter((m) => m.provider === "ollama" && matches(m)),
      cloudModels: models.filter((m) => m.provider === "groq" && matches(m)),
    };
  }, [models, query]);

  return (
    <div ref={rootRef} className="relative">
      {/* Trigger — quiet control, matches app inputs */}
      <button
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className={cn(
          "flex h-9 w-full items-center gap-2 rounded-lg border px-3 text-left transition-colors md:w-64",
          open
            ? "border-primary/50 bg-secondary/60"
            : "border-border/60 bg-transparent hover:bg-surface-hover",
        )}
      >
        <span
          className={cn(
            "h-1.75 w-1.75 shrink-0 rounded-full",
            isInherit
              ? "bg-muted-foreground/40"
              : current
                ? modelDotClass(current)
                : "bg-muted-foreground/40",
          )}
          aria-hidden
        />
        <span className="min-w-0 flex-1 truncate text-[13px] text-foreground">
          {isInherit
            ? inheritOption
            : current
              ? current.description
              : currentModel}
        </span>
        {open ? (
          <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        )}
      </button>

      {/* Dropdown panel */}
      {open && (
        <div className="absolute left-0 right-0 z-50 mt-1.5 overflow-hidden rounded-lg border border-border/70 bg-popover shadow-[0_12px_32px_-8px_var(--color-shadow-strong)] md:left-auto md:w-80">
          {/* Search */}
          <div className="flex h-9 items-center gap-2 border-b border-border/60 px-3">
            <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            <input
              ref={searchRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("settings.ai.models.search")}
              className="flex-1 bg-transparent text-[13px] text-foreground outline-none placeholder:text-muted-foreground/70"
            />
          </div>

          {/* List */}
          <div className="max-h-64 overflow-y-auto py-1">
            {inheritOption && (
              <button
                onClick={() => {
                  onSelect(task, null);
                  setOpen(false);
                }}
                className="flex w-full cursor-pointer items-center gap-2.5 px-3 py-2 text-left transition-colors hover:bg-surface-hover"
              >
                <Sparkles className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1 truncate text-[13px] text-foreground">
                  {inheritOption}
                </span>
                {isInherit && (
                  <Check className="h-3.5 w-3.5 shrink-0 text-primary" />
                )}
              </button>
            )}

            {localModels.length === 0 && cloudModels.length === 0 && (
              <div className="px-3 py-6 text-center text-[12px] text-muted-foreground">
                {t("settings.ai.models.noResults")}
              </div>
            )}

            {localModels.length > 0 && (
              <div className="px-3 pb-1 pt-1.5 text-[10.5px] font-medium uppercase tracking-wider text-muted-foreground/80">
                {t("settings.ai.models.local")}
              </div>
            )}
            {localModels.map((m) => (
              <ModelRow
                key={m.id}
                model={m}
                selected={m.id === currentModel}
                disabled={false}
                onSelect={(id) => {
                  onSelect(task, id);
                  setOpen(false);
                }}
              />
            ))}

            {groqConnected && cloudModels.length > 0 && (
              <div className="mt-1 px-3 pb-1 pt-1.5 text-[10.5px] font-medium uppercase tracking-wider text-muted-foreground/80">
                {t("settings.ai.models.cloud")}
              </div>
            )}
            {groqConnected &&
              cloudModels.map((m) => (
                <ModelRow
                  key={m.id}
                  model={m}
                  selected={m.id === currentModel}
                  disabled={localOnly}
                  reason={
                    localOnly ? t("settings.ai.models.localOnly") : undefined
                  }
                  onSelect={(id) => {
                    onSelect(task, id);
                    setOpen(false);
                  }}
                />
              ))}
          </div>
        </div>
      )}
    </div>
  );
}
