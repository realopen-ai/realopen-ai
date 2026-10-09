export type NotebookKind = "document" | "artifact" | "note" | "deck";
export interface NotebookItem {
  id: string;
  kind: NotebookKind;
  target_id: string;
  title: string;
  selected: boolean;
  status: string | null;
}
export interface Notebook {
  id: string;
  title: string;
  description: string;
  conversation_id: string;
  created_at: string;
  updated_at: string;
  items: NotebookItem[];
}
async function request<T>(
  path = "",
  method = "GET",
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(`/api/learn/notebooks${path}`, {
    method,
    signal,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(
      typeof error.detail === "string"
        ? error.detail
        : `Request failed (${response.status})`,
    );
  }
  return response.status === 204 ? (undefined as T) : response.json();
}
export const notebooksApi = {
  list: (signal?: AbortSignal) =>
    request<Notebook[]>("", "GET", undefined, signal),
  get: (id: string, signal?: AbortSignal) =>
    request<Notebook>(`/${id}`, "GET", undefined, signal),
  create: (title: string) => request<Notebook>("", "POST", { title }),
  update: (id: string, title: string, description: string) =>
    request<Notebook>(`/${id}`, "PUT", { title, description }),
  delete: (id: string) => request<void>(`/${id}`, "DELETE"),
  attach: (id: string, kind: NotebookKind, target_id: string) =>
    request<Notebook>(`/${id}/items`, "POST", { kind, target_id }),
  select: (id: string, itemId: string, selected: boolean) =>
    request<Notebook>(`/${id}/items/${itemId}`, "PATCH", { selected }),
  unlink: (id: string, itemId: string) =>
    request<void>(`/${id}/items/${itemId}`, "DELETE"),
};

export function notebookSourceCount(items: NotebookItem[]) {
  return items.filter(
    (item) =>
      (item.kind === "document" || item.kind === "artifact") && item.selected,
  ).length;
}
