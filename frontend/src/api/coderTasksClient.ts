export interface CoderTask {
  id: string;
  status: string;
  request: string;
  summary: string | null;
  created_at: string;
  completed_at: string | null;
  elapsed_seconds: number;
  stop_after_round: boolean;
  cancellation_requested: boolean;
  resumed_from: string | null;
  can_resume: boolean;
  files_changed: string[];
  progress: {
    round?: number;
    max_rounds?: number;
    phase?: string;
    action?: string;
    model?: string;
    input_tokens?: number;
    input_limit?: number;
    context_window?: number;
    output_reserve?: number;
  };
  plan: {
    steps?: { task: string; status: string }[];
    next_action?: string;
    notes?: string;
  } | null;
  verification: Record<string, { status: string }>;
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Request failed (${response.status})`);
  }
  return response.json();
}
export function listCoderTasks(conversationId: string, signal?: AbortSignal) {
  return request<{ tasks: CoderTask[] }>(
    `/api/coder/tasks?conversation_id=${encodeURIComponent(conversationId)}`,
    { signal },
  );
}
export function controlCoderTask(
  conversationId: string,
  taskId: string,
  action: "stop-after-round" | "cancel",
) {
  return request<CoderTask>(
    `/api/coder/tasks/${encodeURIComponent(taskId)}/${action}?conversation_id=${encodeURIComponent(conversationId)}`,
    { method: "POST" },
  );
}
