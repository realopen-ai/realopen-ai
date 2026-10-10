export interface QuizQuestion {
  id: string;
  type: "multiple_choice" | "short_answer";
  prompt: string;
  options: string[];
  answer?: string;
  correct_option?: number | null;
  explanation?: string;
  source_document_id?: string | null;
  source_page?: number | null;
  source_chunk_id?: string | null;
  source_artifact?: import("./artifactsClient").ArtifactReference | null;
}
export interface Quiz {
  id: string;
  title: string;
  description: string;
  revision: number;
  grounded: boolean;
  question_count: number;
  questions: QuizQuestion[];
  source_conversation_id: string | null;
  source_note_id: string | null;
}
export interface QuizAttempt {
  id: string;
  quiz_id: string;
  snapshot: {
    title: string;
    revision: number;
    questions: QuizQuestion[];
    source_conversation_id?: string | null;
    source_note_id?: string | null;
  };
  answers: Record<string, number | string | null>;
  results: Record<
    string,
    { correct: boolean | null; method: string; feedback: string }
  > | null;
  score: number | null;
  needs_review: number;
  submitted_at: string | null;
  created_at: string;
}
async function request<T>(
  path = "",
  method = "GET",
  body?: unknown,
): Promise<T> {
  const response = await fetch(`/api/learn/quizzes${path}`, {
    method,
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
export const quizzesApi = {
  fromNote: (id: string, preferences: unknown) =>
    request<Quiz>(`/from-note/${id}`, "POST", preferences),
  mistakes: (id: string, attempt: string) =>
    request<{ question_id: string; front: string; back: string }[]>(
      `/${id}/attempts/${attempt}/flashcards`,
    ),
  saveMistakes: (
    id: string,
    attempt: string,
    title: string,
    question_ids: string[],
  ) =>
    request<import("./flashcardsClient").Deck>(
      `/${id}/attempts/${attempt}/flashcards`,
      "POST",
      { title, question_ids },
    ),
  list: (q = "") => request<Quiz[]>(`?q=${encodeURIComponent(q)}`),
  get: (id: string) => request<Quiz>(`/${id}`),
  editor: (id: string) => request<Quiz>(`/${id}/editor`),
  save: (body: unknown, id?: string) =>
    request<Quiz>(id ? `/${id}` : "", id ? "PUT" : "POST", body),
  delete: (id: string) => request<void>(`/${id}`, "DELETE"),
  start: (id: string) => request<QuizAttempt>(`/${id}/attempts`, "POST"),
  attempt: (id: string, attempt: string) =>
    request<QuizAttempt>(`/${id}/attempts/${attempt}`),
  history: (id: string) => request<QuizAttempt[]>(`/${id}/attempts`),
  draft: (id: string, attempt: string, answers: QuizAttempt["answers"]) =>
    request<QuizAttempt>(`/${id}/attempts/${attempt}/answers`, "PUT", {
      answers,
    }),
  submit: (id: string, attempt: string, answers: QuizAttempt["answers"]) =>
    request<QuizAttempt>(`/${id}/attempts/${attempt}/submit`, "POST", {
      answers,
    }),
  override: (id: string, attempt: string, question: string, correct: boolean) =>
    request<QuizAttempt>(
      `/${id}/attempts/${attempt}/override/${question}`,
      "PUT",
      { correct },
    ),
};
