import { useState } from "react";
import type { AppContext } from "./extensions";
import type { AppConfig, Entity, Row } from "./types";
import { label } from "./data";
import { useCrud } from "./hooks/useCrud";
import { RecordForm } from "./components/RecordForm";
import { RecordList } from "./components/RecordList";
import { Filters } from "./components/Filters";

function Resource({
  entity,
  context,
}: {
  entity: Entity;
  context: AppContext;
}) {
  const crud = useCrud(entity, context);
  const [editing, edit] = useState<Row | null>(null);
  const [formVersion, resetForm] = useState(0);
  const { query, page, busy } = crud;
  return (
    <section aria-label={label(entity)} aria-busy={busy}>
      <p role="status" aria-live="polite">
        {busy ? "Loading…" : crud.message}
      </p>
      {crud.error && (
        <p role="alert" className="error">
          {crud.error}{" "}
          <button type="button" disabled={busy} onClick={crud.reload}>
            Retry
          </button>
        </p>
      )}
      <Filters
        entity={entity}
        query={query}
        busy={busy}
        change={crud.setQuery}
      />
      <RecordForm
        key={`${editing?.id ?? "new"}-${formVersion}`}
        entity={entity}
        record={editing}
        busy={busy}
        cancel={() => edit(null)}
        save={async (values) => {
          const success = await crud.mutate(
            editing ? "updated" : "created",
            editing?.id,
            values,
          );
          if (success) {
            edit(null);
            resetForm((value) => value + 1);
          }
          return success;
        }}
      />
      <RecordList
        entity={entity}
        rows={page.items}
        busy={busy}
        edit={edit}
        update={(row, values) => {
          void crud.mutate("updated", row.id, values);
        }}
        remove={(row) => {
          void crud.mutate("deleted", row.id).then((success) => {
            if (success && editing?.id === row.id) edit(null);
          });
        }}
      />
      <nav aria-label="Pagination">
        <button
          type="button"
          disabled={busy || query.offset === 0}
          onClick={() =>
            crud.setQuery({
              ...query,
              offset: Math.max(0, query.offset - query.limit),
            })
          }
        >
          Previous
        </button>
        <span>
          {page.total ? query.offset + 1 : 0}–
          {Math.min(query.offset + query.limit, page.total)} / {page.total}
        </span>
        <button
          type="button"
          disabled={busy || query.offset + query.limit >= page.total}
          onClick={() =>
            crud.setQuery({ ...query, offset: query.offset + query.limit })
          }
        >
          Next
        </button>
      </nav>
    </section>
  );
}

export function App({
  config,
  context,
}: {
  config: AppConfig;
  context: AppContext;
}) {
  const [selected, select] = useState(config.entities[0]!);
  return (
    <main className="container">
      <h1>{config.name}</h1>
      <small>
        {config.local
          ? "Saved in this browser"
          : "Saved to the application database"}
      </small>
      <nav aria-label="Resources">
        {config.entities.map((entity) => (
          <button
            type="button"
            key={entity.name}
            className={entity === selected ? "" : "outline"}
            aria-current={entity === selected}
            onClick={() => select(entity)}
          >
            {label(entity)}
          </button>
        ))}
      </nav>
      <Resource key={selected.name} entity={selected} context={context} />
    </main>
  );
}
