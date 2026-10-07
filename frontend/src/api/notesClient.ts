import type { CardInput, Deck } from "./flashcardsClient";

export interface StudyNote {
  id: string;
  title: string;
  content: string;
  excerpt?: string;
  pinned: boolean;
  source_conversation_id: string | null;
  source_document_id: string | null;
  source_page: number | null;
  source_chunk_id: string | null;
  created_at: string;
  updated_at: string;
}
export type NoteAction =
  "shorter" | "clearer" | "expand" | "bullets" | "flashcards";
export interface NoteProposal {
  content?: string;
  cards?: CardInput[];
}
async function request<T>(
  path = "",
  method = "GET",
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(`/api/learn/notes${path}`, {
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
export const notesApi = {
  list: (q = "", signal?: AbortSignal) =>
    request<StudyNote[]>(
      `?q=${encodeURIComponent(q)}`,
      "GET",
      undefined,
      signal,
    ),
  get: (id: string, signal?: AbortSignal) =>
    request<StudyNote>(`/${id}`, "GET", undefined, signal),
  create: (title: string) =>
    request<StudyNote>("", "POST", { title, content: "" }),
  save: (id: string, title: string, content: string, pinned: boolean) =>
    request<StudyNote>(`/${id}`, "PUT", { title, content, pinned }),
  delete: (id: string) => request<void>(`/${id}`, "DELETE"),
  propose: (id: string, action: NoteAction, selection?: string, count = 10) =>
    request<NoteProposal>(`/${id}/propose`, "POST", {
      action,
      selection,
      count,
    }),
  flashcards: (id: string, title: string, cards: CardInput[]) =>
    request<Deck>(`/${id}/flashcards`, "POST", { title, cards }),
};
