import { useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  Cloud,
  HardDrive,
  Search,
  Sparkles,
} from "lucide-react";

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
        "w-full flex items-center gap-2.5 px-3 py-2 text-left transition-colors",
        disabled
          ? "opacity-50 cursor-not-allowed"
          : "hover:bg-accent cursor-pointer",
      )}
    >
      {/* Provider badge */}
      <span
        className={cn(
          "shrink-0 w-5 h-5 rounded-md flex items-center justify-center",
          model.provider === "groq"
            ? "bg-amber-500/15 text-amber-500"
            : model.installed
              ? "bg-emerald-500/15 text-emerald-500"
              : "bg-secondary text-muted-foreground",
        )}
      >
        {model.provider === "groq" ? (
          <Cloud className="w-3 h-3" />
        ) : (
          <HardDrive className="w-3 h-3" />
        )}
      </span>

      <span className="flex-1 min-w-0">
        <span className="block text-[13px] text-foreground truncate">
          {model.description}
        </span>
        <span className="block text-[11px] text-muted-foreground/60 truncate">
          {model.id}
          {model.provider === "ollama" &&
            !model.installed &&
            ` · ${t("settings.ai.models.notInstalled")}`}
          {model.provider === "groq" && " · cloud"}
        </span>
      </span>

      {disabled && reason ? (
        <span className="shrink-0 text-[10px] text-muted-foreground/50">
          {reason}
        </span>
      ) : selected ? (
        <Check className="shrink-0 w-4 h-4 text-primary" />
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
      {/* Trigger */}
      <button
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className={cn(
          "w-full md:w-64 flex items-center gap-2 px-3 py-2 rounded-xl border transition-colors text-left",
          open
            ? "border-primary/40 ring-1 ring-primary/20 bg-primary/5"
            : "border-border bg-card hover:border-border/80 hover:bg-accent/40",
        )}
      >
        <span
          className={cn(
            "shrink-0 w-1.5 h-1.5 rounded-full",
            isInherit
              ? "bg-muted-foreground/40"
              : current?.provider === "groq"
                ? "bg-amber-500"
                : current?.installed === false
                  ? "bg-muted-foreground/40"
                  : "bg-emerald-500",
          )}
        />
        <span className="flex-1 min-w-0 truncate text-[13px] text-foreground">
          {isInherit
            ? inheritOption
            : current
              ? current.description
              : currentModel}
        </span>
        {open ? (
          <Search className="shrink-0 w-3.5 h-3.5 text-muted-foreground" />
        ) : (
          <ChevronDown className="shrink-0 w-3.5 h-3.5 text-muted-foreground" />
        )}
      </button>

      {/* Dropdown panel */}
      {open && (
        <div className="absolute z-50 mt-1.5 left-0 right-0 md:right-0 md:left-auto md:w-80 rounded-xl border border-border bg-popover shadow-2xl overflow-hidden">
          {/* Search */}
          <div className="flex items-center gap-2 px-3 py-2 border-b border-border/60">
            <Search className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
            <input
              ref={searchRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("settings.ai.models.search")}
              className="flex-1 bg-transparent text-[13px] text-foreground placeholder:text-muted-foreground/50 outline-none"
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
                className="w-full flex items-center gap-2.5 px-3 py-2 text-left hover:bg-accent cursor-pointer"
              >
                <span className="shrink-0 w-5 h-5 rounded-md bg-secondary text-muted-foreground flex items-center justify-center">
                  <Sparkles className="w-3 h-3" />
                </span>
                <span className="flex-1 min-w-0">
                  <span className="block text-[13px] text-foreground truncate">
                    {inheritOption}
                  </span>
                </span>
                {isInherit && (
                  <Check className="shrink-0 w-4 h-4 text-primary" />
                )}
              </button>
            )}

            {localModels.length === 0 && cloudModels.length === 0 && (
              <div className="px-3 py-6 text-center text-[12px] text-muted-foreground/60">
                {t("settings.ai.models.noResults")}
              </div>
            )}

            {localModels.length > 0 && (
              <div className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/50">
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
              <div className="px-3 py-1 mt-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/50">
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
