import { label } from "../data";
import type { Entity, Row, Values } from "../types";

type Props = {
  entity: Entity;
  rows: Row[];
  busy: boolean;
  edit: (row: Row) => void;
  remove: (row: Row) => void;
  update: (row: Row, values: Values) => void;
};

export function RecordList({
  entity,
  rows,
  busy,
  edit,
  remove,
  update,
}: Props) {
  return (
    <section aria-label="Records">
      {!rows.length && <p>No records yet.</p>}
      {rows.map((row) => (
        <article key={row.id} data-id={row.id}>
          <h3>#{row.id}</h3>
          <dl>
            {entity.fields.map((field) => (
              <div key={field.name}>
                <dt>{label(field)}</dt>
                <dd>
                  {field.type === "boolean" ? (
                    <input
                      type="checkbox"
                      aria-label={`${label(field)} for record ${row.id}`}
                      checked={row[field.name] === true}
                      disabled={busy}
                      onChange={(event) =>
                        update(row, { [field.name]: event.target.checked })
                      }
                    />
                  ) : row[field.name] == null ? (
                    "—"
                  ) : (
                    String(row[field.name])
                  )}
                </dd>
              </div>
            ))}
          </dl>
          <button type="button" disabled={busy} onClick={() => edit(row)}>
            Edit
          </button>
          <button
            type="button"
            className="outline secondary"
            disabled={busy}
            onClick={() => remove(row)}
          >
            Delete
          </button>
        </article>
      ))}
    </section>
  );
}
