import { useState, type FormEvent } from "react";
import { label } from "../data";
import type { Entity, Row, Values } from "../types";

type Props = {
  entity: Entity;
  record: Row | null;
  busy: boolean;
  save: (values: Values) => Promise<boolean>;
  cancel: () => void;
};

export function RecordForm({ entity, record, busy, save, cancel }: Props) {
  const [draft, setDraft] = useState<Record<string, string | boolean | null>>(
    () =>
      Object.fromEntries(
        entity.fields.map((field) => {
          const value = record ? record[field.name] : field.default;
          return [
            field.name,
            field.type === "boolean"
              ? value == null
                ? field.required
                  ? false
                  : null
                : value === true
              : value == null
                ? ""
                : String(value),
          ];
        }),
      ),
  );
  const change = (name: string, value: string | boolean | null) =>
    setDraft((current) => ({ ...current, [name]: value }));

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || !event.currentTarget.reportValidity()) return;
    const values: Values = {};
    for (const field of entity.fields) {
      const value = draft[field.name];
      values[field.name] =
        field.type === "boolean"
          ? (value as boolean | null)
          : !String(value).trim() && !field.required
            ? null
            : field.type === "string"
              ? String(value).trim()
              : Number(value);
    }
    await save(values);
  }

  return (
    <form
      aria-label={record ? "Edit record" : "Create record"}
      onSubmit={submit}
    >
      <h2>{record ? "Edit record" : "New record"}</h2>
      {entity.fields.map((field) => (
        <label key={field.name}>
          {label(field)}
          {field.type === "boolean" && !field.required ? (
            <select
              name={field.name}
              disabled={busy}
              value={
                draft[field.name] === null ? "" : String(draft[field.name])
              }
              onChange={(event) =>
                change(
                  field.name,
                  event.target.value === ""
                    ? null
                    : event.target.value === "true",
                )
              }
            >
              <option value="">Not set</option>
              <option value="true">Yes</option>
              <option value="false">No</option>
            </select>
          ) : field.type === "boolean" ? (
            <input
              name={field.name}
              type="checkbox"
              disabled={busy}
              checked={draft[field.name] === true}
              onChange={(event) => change(field.name, event.target.checked)}
            />
          ) : (
            <input
              name={field.name}
              type={field.type === "string" ? "text" : "number"}
              step={field.type === "integer" ? "1" : "any"}
              maxLength={field.type === "string" ? 1000 : undefined}
              required={field.required}
              disabled={busy}
              value={String(draft[field.name] ?? "")}
              onChange={(event) => change(field.name, event.target.value)}
            />
          )}
        </label>
      ))}
      <button type="submit" disabled={busy}>
        {record ? "Save changes" : "Add record"}
      </button>
      {record && (
        <button
          type="button"
          className="outline"
          disabled={busy}
          onClick={cancel}
        >
          Cancel edit
        </button>
      )}
    </form>
  );
}
