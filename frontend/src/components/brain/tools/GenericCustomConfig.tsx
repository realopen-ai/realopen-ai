/**
 * Generic custom-settings renderer (Brain ▸ Tools).
 *
 * Fallback UI for tools that declare a custom_schema but do not (yet)
 * have a dedicated React component: walks the backend's field
 * descriptors and renders toggles / text inputs / number inputs /
 * dropdowns. Secret fields are NOT rendered here — they need dedicated
 * components with the secret-store flow (see WebSearchCustom).
 *
 * This keeps the hybrid architecture: the backend remains the source
 * of truth for what configuration exists; the frontend renders it
 * generically unless a tool deserves bespoke UI.
 */

import { useState } from "react";

import { useToolsStore } from "@/store/toolsStore";
import { useT } from "@/store/settingsStore";
import { cn } from "@/lib/utils";
import type { ToolInfo, ToolConfigField } from "@/api/toolsClient";
import { SettingToggle } from "@/components/brain/ToolsTab";

function setDeep(
  obj: Record<string, unknown>,
  path: string,
  value: unknown,
): Record<string, unknown> {
  // Fields arrive flat with provider-prefixed keys (e.g.
  // "searxng.enabled") — nested under the first dot segment to match
  // the stored custom shape.
  const parts = path.split(".");
  const out: Record<string, unknown> = { ...obj };
  let cursor = out;
  for (const part of parts.slice(0, -1)) {
    const next = { ...((cursor[part] as Record<string, unknown>) ?? {}) };
    cursor[part] = next;
    cursor = next;
  }
  cursor[parts[parts.length - 1]] = value;
  return out;
}

function readDeep(
  obj: Record<string, unknown>,
  path: string,
  fallback: unknown,
): unknown {
  let cursor: unknown = obj;
  for (const part of path.split(".")) {
    if (cursor && typeof cursor === "object" && part in (cursor as object)) {
      cursor = (cursor as Record<string, unknown>)[part];
    } else {
      return fallback;
    }
  }
  return cursor;
}

function FieldRow({
  tool,
  field,
  custom,
}: {
  tool: ToolInfo;
  field: ToolConfigField;
  custom: Record<string, unknown>;
}) {
  const updateConfig = useToolsStore((s) => s.updateConfig);
  const isSaving = !!useToolsStore((s) => s.saving[tool.tool]);

  const current = readDeep(custom, field.key, field.default);

  const commit = (value: unknown) => {
    updateConfig(tool.tool, { custom: setDeep(custom, field.key, value) });
  };

  // Secret fields are skipped — they need the dedicated secret-store
  // flow (masked status, validation, never display).
  if (field.type === "secret") return null;

  return (
    <div className="flex items-center gap-3 py-2.5">
      <div className="flex-1 min-w-0">
        <div className="text-[13px] text-foreground">{field.label}</div>
        {field.help && (
          <div className="text-[11px] text-muted-foreground/70 leading-snug mt-0.5">
            {field.help}
          </div>
        )}
      </div>
      <div className="shrink-0">
        {field.type === "bool" && (
          <SettingToggle
            checked={current === true}
            disabled={isSaving}
            label={field.label}
            onChange={(v) => commit(v)}
          />
        )}
        {field.type === "string" && (
          <input
            type="text"
            defaultValue={(current as string) ?? ""}
            key={`${field.key}-${String(current ?? "")}`}
            placeholder={field.placeholder}
            disabled={isSaving}
            onBlur={(e) => commit(e.target.value)}
            className="w-48 px-3 py-1.5 rounded-lg border border-border bg-card text-[12.5px] text-foreground placeholder:text-muted-foreground/50 outline-none focus:border-primary/40 disabled:opacity-50"
          />
        )}
        {field.type === "int" && (
          <input
            type="number"
            defaultValue={Number(current ?? field.default ?? 1)}
            key={`${field.key}-${String(current ?? "")}`}
            min={1}
            max={60}
            disabled={isSaving}
            onBlur={(e) => {
              const n = parseInt(e.target.value, 10);
              if (!isNaN(n)) commit(n);
            }}
            className="w-20 px-2.5 py-1.5 rounded-lg border border-border bg-card text-[12.5px] text-foreground text-right outline-none focus:border-primary/40 disabled:opacity-50"
          />
        )}
        {field.type === "select" && (
          <select
            value={(current as string) ?? field.options?.[0]?.value ?? ""}
            disabled={isSaving}
            onChange={(e) => commit(e.target.value)}
            className={cn(
              "w-40 px-2.5 py-1.5 rounded-lg border border-border bg-card text-[12.5px]",
              "text-foreground outline-none focus:border-primary/40 disabled:opacity-50",
            )}
          >
            {(field.options ?? []).map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        )}
      </div>
    </div>
  );
}

export function GenericCustomConfig({ tool }: { tool: ToolInfo }) {
  const t = useT();
  const [showRaw, setShowRaw] = useState(false);
  const custom = (tool.config.custom ?? {}) as Record<string, unknown>;

  const sections = tool.custom_schema ?? [];
  if (sections.length === 0) return null;

  return (
    <div className="space-y-2">
      {sections.map((section) => {
        const fields = section.fields.filter((f) => f.type !== "secret");
        if (fields.length === 0) return null;
        return (
          <div
            key={section.key}
            className="rounded-xl border border-border bg-card px-4 divide-y divide-border/50"
          >
            {fields.map((field) => (
              <FieldRow
                key={field.key}
                tool={tool}
                field={field}
                custom={custom}
              />
            ))}
          </div>
        );
      })}
      <button
        onClick={() => setShowRaw((v) => !v)}
        className="text-[11px] text-muted-foreground/60 hover:text-foreground transition-colors"
      >
        {t("brain.tools.showRawConfig")}
      </button>
      {showRaw && (
        <pre className="px-3 py-2.5 rounded-xl border border-border bg-secondary/40 text-[11px] text-muted-foreground overflow-x-auto">
          {JSON.stringify(tool.config.custom, null, 2)}
        </pre>
      )}
    </div>
  );
}
