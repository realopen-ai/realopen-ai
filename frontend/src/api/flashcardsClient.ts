export interface Flashcard {
  source_artifact?: import("./artifactsClient").ArtifactReference | null;
  id: string;
  deck_id: string;
  deck_title?: string;
  source_document_id?: string | null;
  source_conversation_id?: string | null;
  front: string;
  back: string;
  position: number;
  source_reference: string | null;
  source_page?: number | null;
  source_chunk_id?: string | null;
  due_at: string;
  reviews: number;
  interval_days: number;
}
export interface Deck {
  source_artifact?: import("./artifactsClient").ArtifactReference | null;
  source_note_id?: string | null;
  id: string;
  title: string;
  description: string | null;
  source_conversation_id: string | null;
  source_document_id: string | null;
  card_count: number;
  due_count: number;
  next_review_at: string | null;
  last_studied_at?: string | null;
  updated_at: string;
  cards?: Flashcard[];
}
export type Rating = "again" | "hard" | "good" | "easy";
export interface CardInput {
  source_artifact?: import("./artifactsClient").ArtifactReference | null;
  front: string;
  back: string;
  source_reference?: string | null;
  source_page?: number | null;
  source_chunk_id?: string | null;
}

async function request<T>(
  path = "",
  method = "GET",
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(`/api/learn/flashcards${path}`, {
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
export const flashcardsApi = {
  due: (signal?: AbortSignal) =>
    request<{ cards: Flashcard[] }>("/review", "GET", undefined, signal),
  rewrite: (card: CardInput, action: string) =>
    request<{ cards: CardInput[] }>("/rewrite", "POST", { card, action }),
  replace: (deckId: string, cardId: string, cards: CardInput[]) =>
    request<Deck>(`/${deckId}/cards/${cardId}/replace`, "POST", { cards }),
  list: (signal?: AbortSignal) => request<Deck[]>("", "GET", undefined, signal),
  get: (id: string, signal?: AbortSignal) =>
    request<Deck>(`/${id}`, "GET", undefined, signal),
  create: (title: string, description: string, cards: CardInput[] = []) =>
    request<Deck>("", "POST", { title, description, cards }),
  edit: (id: string, title: string, description: string) =>
    request<Deck>(`/${id}`, "PUT", { title, description }),
  delete: (id: string) => request<void>(`/${id}`, "DELETE"),
  addCard: (id: string, card: CardInput) =>
    request<Deck>(`/${id}/cards`, "POST", card),
  editCard: (id: string, cardId: string, card: CardInput) =>
    request<Deck>(`/${id}/cards/${cardId}`, "PUT", card),
  deleteCard: (id: string, cardId: string) =>
    request<void>(`/${id}/cards/${cardId}`, "DELETE"),
  review: (id: string, card: Flashcard, rating: Rating, reviewId: string) =>
    request<{ due_at: string; reviews: number }>(
      `/${id}/cards/${card.id}/reviews`,
      "POST",
      {
        rating,
        review_id: reviewId,
        expected_reviews: card.reviews,
      },
    ),
};
