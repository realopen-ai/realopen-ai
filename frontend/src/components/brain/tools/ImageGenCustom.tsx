/**
 * Image Generation custom configuration (Brain ▸ Tools ▸ Image Generation).
 *
 * The dedicated UI for the image-gen provider matrix. The provider
 * implementation lives in the backend service
 * (app/services/image_gen.py); this component only
 * describes/configures it:
 *
 *   Ollama  — enabled, base URL (empty = the global Ollama URL),
 *             timeout. Local diffusion models are slow, so the
 *             timeout ceiling is generous (up to 7200 s).
 *   ComfyUI — planned provider, NOT yet configurable: rendered as a
 *             static "coming soon" card so users can see the roadmap
 *             (workflow-based local generation) without being able to
 *             change anything.
 *
 * Every change persists through PUT /api/tools/use_image_gen and
 * takes effect at runtime immediately.
 */

import { useState } from "react";
import { Cpu, Workflow } from "lucide-react";

import { useToolsStore } from "@/store/toolsStore";
import { useT } from "@/store/settingsStore";
import { cn } from "@/lib/utils";
import type { ToolInfo } from "@/api/toolsClient";
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

// ─── Main component ────────────────────────────────────────────────

type ProviderCfg = Record<string, unknown>;

export function ImageGenCustom({ tool }: { tool: ToolInfo }) {
  const t = useT();
  const updateConfig = useToolsStore((s) => s.updateConfig);

  const providers = (tool.config.custom?.providers ?? {}) as Record<
    string,
    ProviderCfg
  >;

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

  const ollama = providers.ollama as ProviderCfg;
  const enabled = (p: ProviderCfg) => p?.enabled !== false;

  return (
    <section>
      <SectionHeader title={t("brain.tools.imagegen.providers")} />
      <div className="space-y-3">
        {/* Ollama */}
        <ProviderCard
          icon={<Cpu className="h-4 w-4" />}
          title="Ollama"
          subtitle={t("brain.tools.imagegen.ollamaSubtitle")}
          enabled={enabled(ollama)}
          canToggle={true}
          onToggle={(v) => patchProvider("ollama", { enabled: v })}
        >
          <CardField
            label={t("brain.tools.imagegen.baseUrl")}
            help={t("brain.tools.imagegen.baseUrlHelp")}
          >
            <input
              type="text"
              defaultValue={(ollama?.base_url as string) || ""}
              key={String(ollama?.base_url)}
              placeholder="http://localhost:11434"
              onBlur={(e) =>
                patchProvider("ollama", {
                  base_url: e.target.value.trim() || null,
                })
              }
              className={`${inputClass} w-48`}
            />
          </CardField>
          <NumberField
            label={t("brain.tools.imagegen.timeout")}
            value={Number(ollama?.timeout_s ?? 600)}
            min={1}
            max={7200}
            onCommit={(v) => patchProvider("ollama", { timeout_s: v })}
          />
        </ProviderCard>

        {/* ComfyUI — planned provider, not yet configurable */}
        <ProviderCard
          icon={<Workflow className="h-4 w-4" />}
          title="ComfyUI"
          subtitle={t("brain.tools.imagegen.comfyuiSubtitle")}
          enabled={false}
          canToggle={false}
          onToggle={() => {}}
          badge={
            <span className="shrink-0 rounded-md bg-warning/10 px-1.5 py-0.5 text-[10.5px] font-medium text-warning">
              {t("brain.tools.imagegen.comingSoon")}
            </span>
          }
        />
      </div>
    </section>
  );
}
