/**
 * Generic custom-settings renderer (Brain ▸ Tools).
 *
 * Fallback UI for tools that declare a custom_schema but do not (yet)
 * have a dedicated React component: walks the backend's field
 * descriptors and renders toggles / text inputs / number inputs —
 * int (whole values) or float (decimal step, e.g. rag_search's
 * similarity_cutoff 0–1) — and dropdowns. Secret fields are NOT
 * rendered here — they need dedicated
 * components with the secret-store flow (see WebSearchCustom).
 *
 * This keeps the hybrid architecture: the backend remains the source
 * of truth for what configuration exists; the frontend renders it
 * generically unless a tool deserves bespoke UI.
 */

import { useState } from "react";

import { useToolsStore } from "@/store/toolsStore";
import { useT } from "@/store/settingsStore";
import type { ToolInfo, ToolConfigField } from "@/api/toolsClient";
import { SectionHeader } from "@/components/ui/primitives";
import { SettingRow, SettingToggle } from "@/components/brain/ToolsTab";

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

const inputClass =
  "h-9 rounded-lg border border-border/60 bg-transparent px-3 text-[13px] text-foreground placeholder:text-muted-foreground/70 outline-none transition-colors focus:border-primary/50 focus:ring-2 focus:ring-primary/20 disabled:opacity-50";

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
    <SettingRow label={field.label} help={field.help}>
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
          className={`${inputClass} w-48`}
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
          className={`${inputClass} w-20 text-right`}
        />
      )}
      {field.type === "float" && (
        <input
          type="number"
          step="0.01"
          min={0}
          max={1}
          defaultValue={Number(current ?? field.default ?? 0.5)}
          key={`${field.key}-${String(current ?? "")}`}
          disabled={isSaving}
          onBlur={(e) => {
            const n = parseFloat(e.target.value);
            if (!isNaN(n)) commit(n);
          }}
          className={`${inputClass} w-20 text-right`}
        />
      )}
      {field.type === "select" && (
        <select
          value={(current as string) ?? field.options?.[0]?.value ?? ""}
          disabled={isSaving}
          onChange={(e) => commit(e.target.value)}
          className={`${inputClass} w-44`}
        >
          {(field.options ?? []).map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      )}
    </SettingRow>
  );
}

export function GenericCustomConfig({ tool }: { tool: ToolInfo }) {
  const t = useT();
  const [showRaw, setShowRaw] = useState(false);
  const custom = (tool.config.custom ?? {}) as Record<string, unknown>;

  const sections = tool.custom_schema ?? [];
  if (sections.length === 0) return null;

  return (
    <div className="space-y-9">
      {sections.map((section) => {
        const fields = section.fields.filter((f) => f.type !== "secret");
        if (fields.length === 0) return null;
        return (
          <section key={section.key}>
            <SectionHeader title={section.label} />
            <div className="divide-y divide-border/50">
              {fields.map((field) => (
                <FieldRow
                  key={field.key}
                  tool={tool}
                  field={field}
                  custom={custom}
                />
              ))}
            </div>
          </section>
        );
      })}
      <div>
        <button
          onClick={() => setShowRaw((v) => !v)}
          className="text-[12px] text-muted-foreground/70 transition-colors hover:text-foreground"
        >
          {t("brain.tools.showRawConfig")}
        </button>
        {showRaw && (
          <pre className="mt-2 overflow-x-auto rounded-lg bg-secondary/60 px-3 py-2.5 font-mono text-[11px] leading-relaxed text-muted-foreground">
            {JSON.stringify(tool.config.custom, null, 2)}
          </pre>
        )}
      </div>
    </div>
  );
}
