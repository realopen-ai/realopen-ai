import { useState } from "react";
import { label } from "../data";
import type { Entity, Query } from "../types";

export function Filters({
  entity,
  query,
  busy,
  change,
}: {
  entity: Entity;
  query: Query;
  busy: boolean;
  change: (value: Query) => void;
}) {
  const [search, setSearch] = useState(query.search);
  const [sort, setSort] = useState(query.sort);
  return (
    <form
      className="toolbar"
      onSubmit={(event) => {
        event.preventDefault();
        change({ ...query, search, sort, offset: 0 });
      }}
    >
      <input
        type="search"
        aria-label="Search records"
        placeholder="Search…"
        value={search}
        disabled={busy}
        onChange={(event) => setSearch(event.target.value)}
      />
      <select
        aria-label="Sort by"
        value={sort}
        disabled={busy}
        onChange={(event) => setSort(event.target.value)}
      >
        <option value="id">ID</option>
        {entity.fields.map((field) => (
          <option key={field.name} value={field.name}>
            {label(field)}
          </option>
        ))}
      </select>
      <button type="submit" disabled={busy}>
        Search
      </button>
      <button
        type="button"
        disabled={busy}
        onClick={() => change({ ...query, descending: !query.descending })}
      >
        {query.descending ? "Descending" : "Ascending"}
      </button>
    </form>
  );
}
