import { createContext, type AppContext } from "./extensions";
import type { AppConfig, Row } from "./types";

export const label = (value: { name: string; label: string | null }) =>
  value.label || value.name.replaceAll("_", " ");

// Frontend-only apps explicitly use browser persistence. Full-stack apps never
// silently switch to localStorage when their real API is unavailable.
function localRequest(config: AppConfig): AppContext["request"] {
  const base = `realopen:${config.name}:v1:`;
  return async <T,>(path: string, init?: RequestInit): Promise<T> => {
    const url = new URL(path, "http://workspace.invalid");
    const [, , entityName, recordId] = url.pathname.split("/");
    const entity = config.entities.find(
      (candidate) => candidate.name === entityName,
    );
    if (!entity) throw new Error("Unknown resource");
    const key = base + entity.name;
    const stored: Row[] = JSON.parse(localStorage.getItem(key) || "[]");
    const id = Number(recordId),
      method = init?.method || "GET";
    let result: unknown;
    if (method === "GET" && recordId) {
      result = stored.find((row) => row.id === id);
      if (!result) throw new Error("Record not found");
    } else if (method === "GET") {
      const search = (url.searchParams.get("search") || "").toLowerCase();
      const sort = url.searchParams.get("sort") || "id";
      const descending = url.searchParams.get("descending") === "true";
      const filtered = stored.filter(
        (row) =>
          !search ||
          entity.fields.some(
            (f) =>
              f.type === "string" &&
              String(row[f.name] ?? "")
                .toLowerCase()
                .includes(search),
          ),
      );
      filtered.sort((a, b) => {
        const left = a[sort],
          right = b[sort];
        const compare =
          left === right
            ? 0
            : left === null
              ? -1
              : right === null
                ? 1
                : left! < right!
                  ? -1
                  : 1;
        return (descending ? -compare : compare) || a.id - b.id;
      });
      const limit = Number(url.searchParams.get("limit") || 25),
        offset = Number(url.searchParams.get("offset") || 0);
      result = {
        items: filtered.slice(offset, offset + limit),
        total: filtered.length,
        limit,
        offset,
      };
    } else if (method === "POST") {
      const sequenceKey = key + ":sequence";
      const next =
        stored.reduce(
          (maximum, row) => Math.max(maximum, row.id),
          Number(localStorage.getItem(sequenceKey) || 0),
        ) + 1;
      result = { ...JSON.parse(String(init?.body)), id: next };
      stored.push(result as Row);
      localStorage.setItem(sequenceKey, String(next));
    } else {
      const index = stored.findIndex((row) => row.id === id);
      if (index < 0) throw new Error("Record not found");
      if (method === "DELETE") stored.splice(index, 1);
      else
        result = stored[index] = {
          ...stored[index],
          ...JSON.parse(String(init?.body)),
          id,
        };
    }
    if (method !== "GET") localStorage.setItem(key, JSON.stringify(stored));
    return result as T;
  };
}

export function createCrudContext(config: AppConfig) {
  return createContext(config.local ? localRequest(config) : undefined);
}
