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
import { Button } from "@/components/ui/button";
import { SectionHeader } from "@/components/ui/primitives";
import { SettingToggle } from "@/components/brain/ToolsTab";

// ─── Shared input styling ──────────────────────────────────────────

const inputClass =
  "h-9 rounded-lg border border-border/60 bg-transparent px-3 text-[13px] text-foreground placeholder:text-muted-foreground/70 outline-none transition-colors focus:border-primary/50 focus:ring-2 focus:ring-primary/20 disabled:opacity-50";

// ─── Provider card shell ───────────────────────────────────────────

function ProviderCard({
  icon,
  title,
  subtitle,
  enabled,
  onToggle,
  canToggle,
  badge,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  subtitle: string;
  enabled: boolean;
  onToggle: (v: boolean) => void;
  canToggle: boolean;
  /** Optional element shown instead of the toggle (e.g. "Coming soon"). */
  badge?: React.ReactNode;
  children?: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "rounded-xl border border-border/60 bg-card p-4 transition-opacity",
        !enabled && "opacity-70",
      )}
    >
      <div className="flex items-center gap-3">
        <span
          className={cn(
            "flex h-8 w-8 shrink-0 items-center justify-center rounded-lg",
            enabled
              ? "bg-primary/10 text-primary"
              : "bg-secondary text-muted-foreground/80",
          )}
        >
          {icon}
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-[13.5px] font-medium text-foreground">
            {title}
          </div>
          <div className="truncate text-xs text-muted-foreground/80">
            {subtitle}
          </div>
        </div>
        {badge ??
          (canToggle && (
            <SettingToggle
              checked={enabled}
              label={title}
              onChange={onToggle}
            />
          ))}
      </div>
      {enabled && children && (
        <div className="mt-3 divide-y divide-border/50 border-t border-border/50 pt-1">
          {children}
        </div>
      )}
    </div>
  );
}

// ─── One label + control row inside a provider card ────────────────

function CardField({
  label,
  help,
  children,
}: {
  label: string;
  help?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center gap-4 py-2.5">
      <div className="min-w-0 flex-1">
        <div className="text-[13px] text-foreground">{label}</div>
        {help && (
          <div className="mt-0.5 text-xs leading-relaxed text-muted-foreground/70">
            {help}
          </div>
        )}
      </div>
      <div className="shrink-0">{children}</div>
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
    <CardField label={label}>
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
        className={`${inputClass} w-20 text-right`}
      />
    </CardField>
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
    <div className="space-y-2 py-2.5">
      <div className="flex items-center gap-2">
        <KeyRound className="h-3.5 w-3.5 shrink-0 text-muted-foreground/80" />
        <span className="text-[13px] text-foreground">{label}</span>
        {status.set && !editing && (
          <span className="truncate rounded-md bg-secondary px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
            {status.masked}
          </span>
        )}
      </div>
      {status.set && !editing ? (
        <div className="flex items-center gap-1.5">
          <Button variant="ghost" size="xs" onClick={() => setEditing(true)}>
            {t("brain.tools.websearch.updateKey")}
          </Button>
          <Button
            variant="ghost"
            size="xs"
            className="text-danger hover:bg-danger/10 hover:text-danger"
            onClick={() => {
              onSave(null);
              setEditing(false);
              setValue("");
            }}
          >
            {t("brain.tools.websearch.removeKey")}
          </Button>
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
            className={`${inputClass} w-full sm:max-w-xs`}
          />
          <Button
            size="sm"
            onClick={() => {
              if (value.trim()) {
                onSave(value.trim());
                setValue("");
                setEditing(false);
              }
            }}
            disabled={!value.trim() || isSaving}
          >
            {isSaving ? <Loader2 className="animate-spin" /> : <Check />}
            {t("brain.tools.websearch.saveKey")}
          </Button>
        </div>
      )}
      <p className="text-xs leading-relaxed text-muted-foreground/70">{help}</p>
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
    <section>
      <SectionHeader title={t("brain.tools.websearch.providers")} />
      <div className="space-y-3">
        {/* SearXNG */}
        <ProviderCard
          icon={<Search className="h-4 w-4" />}
          title="SearXNG"
          subtitle={t("brain.tools.websearch.searxngSubtitle")}
          enabled={enabled(searxng)}
          canToggle={true}
          onToggle={(v) => patchProvider("searxng", { enabled: v })}
        >
          <CardField label={t("brain.tools.websearch.baseUrl")}>
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
              className={`${inputClass} w-48`}
            />
          </CardField>
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
        </ProviderCard>

        {/* ArXiv */}
        <ProviderCard
          icon={<FlaskConical className="h-4 w-4" />}
          title="ArXiv"
          subtitle={t("brain.tools.websearch.arxivSubtitle")}
          enabled={enabled(arxiv)}
          canToggle={true}
          onToggle={(v) => patchProvider("arxiv", { enabled: v })}
        >
          <CardField label={t("brain.tools.websearch.subject")}>
            <select
              value={(arxiv?.subject as string) || ""}
              onChange={(e) =>
                patchProvider("arxiv", { subject: e.target.value })
              }
              className={`${inputClass} w-44`}
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
          </CardField>
          <CardField label={t("brain.tools.websearch.sortBy")}>
            <select
              value={(arxiv?.sort_by as string) || "relevance"}
              onChange={(e) =>
                patchProvider("arxiv", { sort_by: e.target.value })
              }
              className={`${inputClass} w-44`}
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
          </CardField>
          <NumberField
            label={t("brain.tools.websearch.maxResults")}
            value={Number(arxiv?.max_results ?? 5)}
            min={1}
            max={10}
            onCommit={(v) => patchProvider("arxiv", { max_results: v })}
          />
        </ProviderCard>

        {/* Wikipedia */}
        <ProviderCard
          icon={<BookOpen className="h-4 w-4" />}
          title="Wikipedia"
          subtitle={t("brain.tools.websearch.wikipediaSubtitle")}
          enabled={enabled(wikipedia)}
          canToggle={true}
          onToggle={(v) => patchProvider("wikipedia", { enabled: v })}
        >
          <CardField label={t("brain.tools.websearch.language")}>
            <select
              value={(wikipedia?.language as string) || "en"}
              onChange={(e) =>
                patchProvider("wikipedia", { language: e.target.value })
              }
              className={`${inputClass} w-36`}
            >
              <option value="en">English</option>
              <option value="fr">Français</option>
              <option value="de">Deutsch</option>
              <option value="es">Español</option>
              <option value="pt-br">Português (BR)</option>
              <option value="ar">العربية</option>
            </select>
          </CardField>
          <NumberField
            label={t("brain.tools.websearch.maxResults")}
            value={Number(wikipedia?.max_results ?? 3)}
            min={1}
            max={10}
            onCommit={(v) => patchProvider("wikipedia", { max_results: v })}
          />
        </ProviderCard>

        {/* Google */}
        <ProviderCard
          icon={<Globe className="h-4 w-4" />}
          title="Google"
          subtitle={t("brain.tools.websearch.googleSubtitle")}
          enabled={enabled(google)}
          canToggle={true}
          onToggle={(v) => patchProvider("google", { enabled: v })}
        >
          <CardField label={t("brain.tools.websearch.cseId")}>
            <input
              type="text"
              defaultValue={(google?.cse_id as string) || ""}
              key={String(google?.cse_id)}
              placeholder="0123456789abcdef:xyz"
              onBlur={(e) =>
                patchProvider("google", { cse_id: e.target.value.trim() })
              }
              className={`${inputClass} w-48`}
            />
          </CardField>
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
          <div className="pt-2.5">
            <a
              href="https://programmablesearchengine.google.com/"
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-[11.5px] text-muted-foreground transition-colors hover:text-primary"
            >
              {t("brain.tools.websearch.googleSetup")}
              <ExternalLink className="h-3 w-3" />
            </a>
          </div>
        </ProviderCard>
      </div>
    </section>
  );
}
