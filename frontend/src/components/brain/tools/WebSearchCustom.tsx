/**
 * Web Search custom configuration (Brain ▸ Tools ▸ Web Search).
 *
 * The dedicated UI for the web-search provider matrix. The provider
 * implementation lives in the backend service
 * (app/services/web_search.py); this component only
 * describes/configures it:
 *
 *   SearxNG   — enabled, base URL (empty = default), max results, timeout
 *   ArXiv     — enabled, subject (arXiv category, e.g. cs.AI), sort
 *               order, max results. arXiv's API is subject-based —
 *               searches are built from the category + keywords, never
 *               a whole sentence.
 *   Wikipedia — enabled, language, max results
 *   Google    — enabled, CSE id, max results + API key (secret store:
 *               validated with a mini request, saved locally, never
 *               displayed again)
 *
 * Every change persists through PUT /api/tools/use_websearch and takes
 * effect at runtime immediately.
 */

import { useState } from "react";
import {
  BookOpen,
  Check,
  ExternalLink,
  FlaskConical,
  Globe,
  KeyRound,
  Loader2,
  Search,
} from "lucide-react";

import { useToolsStore } from "@/store/toolsStore";
import { useT } from "@/store/settingsStore";
import { cn } from "@/lib/utils";
import type { ToolInfo } from "@/api/toolsClient";
import { SettingToggle } from "@/components/brain/ToolsTab";

// ─── Provider card shell ───────────────────────────────────────────

function ProviderCard({
  icon,
  title,
  subtitle,
  enabled,
  onToggle,
  canToggle,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  subtitle: string;
  enabled: boolean;
  onToggle: (v: boolean) => void;
  canToggle: boolean;
  children?: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "rounded-xl border bg-card px-4 py-3 space-y-3 transition-opacity",
        !enabled && "opacity-70",
      )}
    >
      <div className="flex items-center gap-3">
        <span
          className={cn(
            "shrink-0 w-8 h-8 rounded-lg flex items-center justify-center",
            enabled
              ? "bg-primary/10 text-primary"
              : "bg-secondary text-muted-foreground/60",
          )}
        >
          {icon}
        </span>
        <div className="flex-1 min-w-0">
          <div className="text-[13px] font-medium text-foreground">{title}</div>
          <div className="text-[11.5px] text-muted-foreground/80 truncate">
            {subtitle}
          </div>
        </div>
        {canToggle && (
          <SettingToggle checked={enabled} label={title} onChange={onToggle} />
        )}
      </div>
      {enabled && children}
    </div>
  );
}

// ─── Numeric field ─────────────────────────────────────────────────

function NumberField({
  label,
  value,
  onCommit,
  min = 1,
  max = 60,
}: {
  label: string;
  value: number;
  onCommit: (v: number) => void;
  min?: number;
  max?: number;
}) {
  const [local, setLocal] = useState(String(value));
  return (
    <label className="flex items-center gap-3">
      <span className="flex-1 text-[12.5px] text-foreground">{label}</span>
      <input
        type="number"
        min={min}
        max={max}
        value={local}
        onChange={(e) => setLocal(e.target.value)}
        onBlur={() => {
          const n = Math.max(min, Math.min(max, parseInt(local, 10) || min));
          setLocal(String(n));
          if (n !== value) onCommit(n);
        }}
        className="w-20 px-2.5 py-1.5 rounded-lg border border-border bg-card text-[13px] text-foreground text-right outline-none focus:border-primary/40"
      />
    </label>
  );
}

// ─── Secret field (password-style, never displays the stored value) ─

function SecretField({
  label,
  help,
  placeholder,
  status,
  isSaving,
  onSave,
}: {
  label: string;
  help: string;
  placeholder: string;
  status: { set: boolean; masked?: string };
  isSaving: boolean;
  onSave: (value: string | null) => void;
}) {
  const t = useT();
  const [value, setValue] = useState("");
  const [editing, setEditing] = useState(!status.set);

  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2">
        <KeyRound className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
        <span className="text-[12.5px] text-foreground">{label}</span>
        {status.set && !editing && (
          <span className="text-[11px] text-muted-foreground/70 truncate">
            {status.masked}
          </span>
        )}
      </div>
      {status.set && !editing ? (
        <div className="flex items-center gap-2">
          <button
            onClick={() => setEditing(true)}
            className="px-2.5 py-1.5 rounded-lg text-[12px] text-primary hover:bg-primary/10 transition-colors"
          >
            {t("brain.tools.websearch.updateKey")}
          </button>
          <button
            onClick={() => {
              onSave(null);
              setEditing(false);
              setValue("");
            }}
            className="px-2.5 py-1.5 rounded-lg text-[12px] text-muted-foreground hover:text-red-400 hover:bg-red-500/10 transition-colors"
          >
            {t("brain.tools.websearch.removeKey")}
          </button>
        </div>
      ) : (
        <div className="flex gap-2">
          <input
            type="password"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={placeholder}
            autoComplete="off"
            spellCheck={false}
            className="flex-1 px-3 py-2 rounded-xl border border-border bg-card text-[13px] text-foreground placeholder:text-muted-foreground/50 outline-none focus:border-primary/40 focus:ring-1 focus:ring-primary/20"
          />
          <button
            onClick={() => {
              if (value.trim()) {
                onSave(value.trim());
                setValue("");
                setEditing(false);
              }
            }}
            disabled={!value.trim() || isSaving}
            className={cn(
              "shrink-0 flex items-center gap-1.5 px-3.5 py-2 rounded-xl text-[12px] font-medium transition-colors",
              !value.trim() || isSaving
                ? "bg-secondary text-muted-foreground cursor-not-allowed"
                : "bg-primary text-primary-foreground hover:opacity-90",
            )}
          >
            {isSaving ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Check className="w-3.5 h-3.5" />
            )}
            {t("brain.tools.websearch.saveKey")}
          </button>
        </div>
      )}
      <p className="text-[11px] text-muted-foreground/70 leading-relaxed">
        {help}
      </p>
    </div>
  );
}

// ─── Main component ────────────────────────────────────────────────

type ProviderCfg = Record<string, unknown>;

export function WebSearchCustom({ tool }: { tool: ToolInfo }) {
  const t = useT();
  const updateConfig = useToolsStore((s) => s.updateConfig);
  const updateSecret = useToolsStore((s) => s.updateSecret);
  const isSaving = !!useToolsStore((s) => s.saving[tool.tool]);

  const providers = (tool.config.custom?.providers ?? {}) as Record<
    string,
    ProviderCfg
  >;
  const googleStatus = tool.secrets["google_api_key"] ?? { set: false };

  // Subject + sort options come from the backend schema (the backend
  // is the source of truth for the configuration structure).
  const schemaField = (key: string) =>
    (tool.custom_schema ?? [])
      .flatMap((s) => s.fields)
      .find((f) => f.key === key);
  const subjectOptions =
    schemaField("providers.arxiv.subject")?.options ??
    ([] as { value: string; label: string }[]);
  const sortOptions = schemaField("providers.arxiv.sort_by")?.options ?? [
    { value: "relevance", label: t("brain.tools.websearch.sortRelevance") },
    {
      value: "submittedDate",
      label: t("brain.tools.websearch.sortNewest"),
    },
  ];

  const patchProvider = (name: string, patch: ProviderCfg) => {
    updateConfig(tool.tool, {
      custom: {
        providers: {
          ...providers,
          [name]: { ...providers[name], ...patch },
        },
      },
    });
  };

  const searxng = providers.searxng as ProviderCfg;
  const arxiv = providers.arxiv as ProviderCfg;
  const wikipedia = providers.wikipedia as ProviderCfg;
  const google = providers.google as ProviderCfg;

  const enabled = (p: ProviderCfg) => p?.enabled !== false;

  return (
    <div className="space-y-3">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/60">
        {t("brain.tools.websearch.providers")}
      </div>

      {/* SearXNG */}
      <ProviderCard
        icon={<Search className="w-4 h-4" />}
        title="SearXNG"
        subtitle={t("brain.tools.websearch.searxngSubtitle")}
        enabled={enabled(searxng)}
        canToggle={true}
        onToggle={(v) => patchProvider("searxng", { enabled: v })}
      >
        <div className="space-y-2 pt-1 border-t border-border/40">
          <label className="flex items-center gap-3">
            <span className="flex-1 text-[12.5px] text-foreground">
              {t("brain.tools.websearch.baseUrl")}
            </span>
            <input
              type="text"
              defaultValue={(searxng?.base_url as string) || ""}
              key={String(searxng?.base_url)}
              placeholder="http://searxng:8080"
              onBlur={(e) =>
                patchProvider("searxng", {
                  base_url: e.target.value.trim() || null,
                })
              }
              className="w-48 px-3 py-1.5 rounded-lg border border-border bg-card text-[12.5px] text-foreground placeholder:text-muted-foreground/50 outline-none focus:border-primary/40"
            />
          </label>
          <NumberField
            label={t("brain.tools.websearch.maxResults")}
            value={Number(searxng?.max_results ?? 5)}
            min={1}
            max={20}
            onCommit={(v) => patchProvider("searxng", { max_results: v })}
          />
          <NumberField
            label={t("brain.tools.websearch.timeout")}
            value={Number(searxng?.timeout_s ?? 15)}
            min={1}
            max={60}
            onCommit={(v) => patchProvider("searxng", { timeout_s: v })}
          />
        </div>
      </ProviderCard>

      {/* ArXiv */}
      <ProviderCard
        icon={<FlaskConical className="w-4 h-4" />}
        title="ArXiv"
        subtitle={t("brain.tools.websearch.arxivSubtitle")}
        enabled={enabled(arxiv)}
        canToggle={true}
        onToggle={(v) => patchProvider("arxiv", { enabled: v })}
      >
        <div className="space-y-2 pt-1 border-t border-border/40">
          <label className="flex items-center gap-3">
            <span className="flex-1 text-[12.5px] text-foreground">
              {t("brain.tools.websearch.subject")}
            </span>
            <select
              value={(arxiv?.subject as string) || ""}
              onChange={(e) =>
                patchProvider("arxiv", { subject: e.target.value })
              }
              className="w-44 px-2.5 py-1.5 rounded-lg border border-border bg-card text-[12.5px] text-foreground outline-none focus:border-primary/40"
            >
              {(subjectOptions.length
                ? subjectOptions
                : [{ value: "", label: "All subjects" }]
              ).map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-3">
            <span className="flex-1 text-[12.5px] text-foreground">
              {t("brain.tools.websearch.sortBy")}
            </span>
            <select
              value={(arxiv?.sort_by as string) || "relevance"}
              onChange={(e) =>
                patchProvider("arxiv", { sort_by: e.target.value })
              }
              className="w-44 px-2.5 py-1.5 rounded-lg border border-border bg-card text-[12.5px] text-foreground outline-none focus:border-primary/40"
            >
              {sortOptions.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.value === "relevance"
                    ? t("brain.tools.websearch.sortRelevance")
                    : o.value === "submittedDate"
                      ? t("brain.tools.websearch.sortNewest")
                      : o.label}
                </option>
              ))}
            </select>
          </label>
          <NumberField
            label={t("brain.tools.websearch.maxResults")}
            value={Number(arxiv?.max_results ?? 5)}
            min={1}
            max={10}
            onCommit={(v) => patchProvider("arxiv", { max_results: v })}
          />
        </div>
      </ProviderCard>

      {/* Wikipedia */}
      <ProviderCard
        icon={<BookOpen className="w-4 h-4" />}
        title="Wikipedia"
        subtitle={t("brain.tools.websearch.wikipediaSubtitle")}
        enabled={enabled(wikipedia)}
        canToggle={true}
        onToggle={(v) => patchProvider("wikipedia", { enabled: v })}
      >
        <div className="space-y-2 pt-1 border-t border-border/40">
          <label className="flex items-center gap-3">
            <span className="flex-1 text-[12.5px] text-foreground">
              {t("brain.tools.websearch.language")}
            </span>
            <select
              value={(wikipedia?.language as string) || "en"}
              onChange={(e) =>
                patchProvider("wikipedia", { language: e.target.value })
              }
              className="w-32 px-2.5 py-1.5 rounded-lg border border-border bg-card text-[12.5px] text-foreground outline-none focus:border-primary/40"
            >
              <option value="en">English</option>
              <option value="fr">Français</option>
              <option value="de">Deutsch</option>
              <option value="es">Español</option>
              <option value="pt-br">Português (BR)</option>
              <option value="ar">العربية</option>
            </select>
          </label>
          <NumberField
            label={t("brain.tools.websearch.maxResults")}
            value={Number(wikipedia?.max_results ?? 3)}
            min={1}
            max={10}
            onCommit={(v) => patchProvider("wikipedia", { max_results: v })}
          />
        </div>
      </ProviderCard>

      {/* Google */}
      <ProviderCard
        icon={<Globe className="w-4 h-4" />}
        title="Google"
        subtitle={t("brain.tools.websearch.googleSubtitle")}
        enabled={enabled(google)}
        canToggle={true}
        onToggle={(v) => patchProvider("google", { enabled: v })}
      >
        <div className="space-y-3 pt-1 border-t border-border/40">
          <label className="flex items-center gap-3">
            <span className="flex-1 text-[12.5px] text-foreground">
              {t("brain.tools.websearch.cseId")}
            </span>
            <input
              type="text"
              defaultValue={(google?.cse_id as string) || ""}
              key={String(google?.cse_id)}
              placeholder="0123456789abcdef:xyz"
              onBlur={(e) =>
                patchProvider("google", { cse_id: e.target.value.trim() })
              }
              className="w-48 px-3 py-1.5 rounded-lg border border-border bg-card text-[12.5px] text-foreground placeholder:text-muted-foreground/50 outline-none focus:border-primary/40"
            />
          </label>
          <NumberField
            label={t("brain.tools.websearch.maxResults")}
            value={Number(google?.max_results ?? 5)}
            min={1}
            max={10}
            onCommit={(v) => patchProvider("google", { max_results: v })}
          />
          <SecretField
            label={t("brain.tools.websearch.apiKey")}
            help={t("brain.tools.websearch.apiKeyHelp")}
            placeholder="AIza…"
            status={googleStatus}
            isSaving={isSaving}
            onSave={(value) => updateSecret(tool.tool, "google_api_key", value)}
          />
          <a
            href="https://programmablesearchengine.google.com/"
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-[11.5px] text-muted-foreground hover:text-primary transition-colors"
          >
            {t("brain.tools.websearch.googleSetup")}
            <ExternalLink className="w-3 h-3" />
          </a>
        </div>
      </ProviderCard>
    </div>
  );
}
