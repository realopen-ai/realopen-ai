import { useCallback, useEffect, useRef, useState } from "react";
import type { AppContext, Mutation } from "../extensions";
import type { Entity, Page, Query, Row, Values } from "../types";

const initialQuery: Query = {
  search: "",
  sort: "id",
  descending: false,
  limit: 25,
  offset: 0,
};
const emptyPage: Page = { items: [], total: 0, limit: 25, offset: 0 };

export function useCrud(entity: Entity, context: AppContext) {
  const [query, setQuery] = useState<Query>({ ...initialQuery });
  const [page, setPage] = useState<Page>(emptyPage);
  const [revision, refresh] = useState(0);
  const [loading, setLoading] = useState(true);
  const [settledRequest, setSettledRequest] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const mutationPending = useRef(false);
  const alive = useRef(true);
  const path = "/api/" + entity.name;
  const requestKey = JSON.stringify([path, query, revision]);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    const previous = context.invalidate;
    const invalidate = async () => {
      refresh((value) => value + 1);
    };
    context.invalidate = invalidate;
    return () => {
      if (context.invalidate === invalidate) context.invalidate = previous;
    };
  }, [context]);

  useEffect(() => {
    let current = true;
    const controller = new AbortController();
    const parameters = new URLSearchParams(
      Object.entries(query).map(([key, value]) => [key, String(value)]),
    );
    setLoading(true);
    setError("");
    context
      .request<Page>(path + "?" + parameters, { signal: controller.signal })
      .then((result) => {
        if (!current) return;
        if (!result.items.length && result.total > 0 && query.offset > 0) {
          setQuery((value) => ({
            ...value,
            offset: Math.max(0, value.offset - value.limit),
          }));
        } else setPage(result);
      })
      .catch((reason) => {
        if (current)
          setError(reason instanceof Error ? reason.message : "Request failed");
      })
      .finally(() => {
        if (current) {
          setLoading(false);
          setSettledRequest(requestKey);
        }
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [context, path, query, revision, requestKey]);

  const mutate = useCallback(
    async (
      action: Mutation["action"],
      id?: number,
      values?: Values,
    ): Promise<boolean> => {
      if (mutationPending.current || !alive.current) return false;
      mutationPending.current = true;
      setSaving(true);
      setError("");
      setMessage("");
      try {
        const record = await context.request<Row | undefined>(
          path + (id === undefined ? "" : `/${id}`),
          {
            method:
              action === "created"
                ? "POST"
                : action === "updated"
                  ? "PATCH"
                  : "DELETE",
            ...(values === undefined
              ? {}
              : {
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify(values),
                }),
          },
        );
        const event: Mutation = {
          entity: entity.name,
          action,
          id: id ?? record!.id,
        };
        for (const listener of context.mutations) {
          try {
            await listener(event);
          } catch (reason) {
            console.warn("Mutation listener failed", reason);
          }
        }
        if (alive.current) {
          setMessage(action === "deleted" ? "Deleted" : "Saved");
          refresh((value) => value + 1);
        }
        return true;
      } catch (reason) {
        if (alive.current)
          setError(reason instanceof Error ? reason.message : "Request failed");
        return false;
      } finally {
        mutationPending.current = false;
        if (alive.current) setSaving(false);
      }
    },
    [context, path, entity.name],
  );

  return {
    query,
    setQuery,
    page,
    busy: loading || saving || settledRequest !== requestKey,
    error,
    message,
    mutate,
    reload: () => refresh((value) => value + 1),
  };
}
