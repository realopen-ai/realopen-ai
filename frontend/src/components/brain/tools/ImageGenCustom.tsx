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
 *             (workflow-based local generation) without being able
 *             to change anything.
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
import { SettingToggle } from "@/components/brain/ToolsTab";

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
        {badge ??
          (canToggle && (
            <SettingToggle
              checked={enabled}
              label={title}
              onChange={onToggle}
            />
          ))}
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
    <div className="space-y-3">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/60">
        {t("brain.tools.imagegen.providers")}
      </div>

      {/* Ollama */}
      <ProviderCard
        icon={<Cpu className="w-4 h-4" />}
        title="Ollama"
        subtitle={t("brain.tools.imagegen.ollamaSubtitle")}
        enabled={enabled(ollama)}
        canToggle={true}
        onToggle={(v) => patchProvider("ollama", { enabled: v })}
      >
        <div className="space-y-2 pt-1 border-t border-border/40">
          <div>
            <label className="flex items-center gap-3">
              <span className="flex-1 text-[12.5px] text-foreground">
                {t("brain.tools.imagegen.baseUrl")}
              </span>
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
                className="w-48 px-3 py-1.5 rounded-lg border border-border bg-card text-[12.5px] text-foreground placeholder:text-muted-foreground/50 outline-none focus:border-primary/40"
              />
            </label>
            <p className="text-[11px] text-muted-foreground/70 leading-relaxed mt-1">
              {t("brain.tools.imagegen.baseUrlHelp")}
            </p>
          </div>
          <NumberField
            label={t("brain.tools.imagegen.timeout")}
            value={Number(ollama?.timeout_s ?? 600)}
            min={1}
            max={7200}
            onCommit={(v) => patchProvider("ollama", { timeout_s: v })}
          />
        </div>
      </ProviderCard>

      {/* ComfyUI — planned provider, not yet configurable */}
      <ProviderCard
        icon={<Workflow className="w-4 h-4" />}
        title="ComfyUI"
        subtitle={t("brain.tools.imagegen.comfyuiSubtitle")}
        enabled={false}
        canToggle={false}
        onToggle={() => {}}
        badge={
          <span className="shrink-0 text-[10.5px] font-medium px-1.5 py-0.5 rounded-full bg-amber-500/10 text-amber-500">
            {t("brain.tools.imagegen.comingSoon")}
          </span>
        }
      />
    </div>
  );
}
