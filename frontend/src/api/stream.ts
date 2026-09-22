import type { ToolCallResult } from "@/store/chatStore";
import type { RetrievedSourceDTO } from "@/api/documentsClient";
import { dbgError, createDebugLogger } from "@/lib/debug";

const log = createDebugLogger("stream");

export interface StreamCallbacks {
  onToken: (token: string) => void;
  onThinkingStart: () => void;
  onThinkingToken: (token: string) => void;
  onThinkingDone: (durationSeconds: number) => void;
  onGenerationDone: (data: {
    thinkingDuration?: number;
    generationDuration: number;
  }) => void;
  onToolCallStart: (toolCall: ToolCallResult) => void;
  onToolCallUpdate: (
    toolCallId: string,
    updates: Partial<ToolCallResult>,
  ) => void;
  onMemoryExtractionStart?: () => void;
  onMemoryExtractionDone?: (data: {
    count: number;
    ran: boolean;
    pending?: boolean;
  }) => void;
  /** Auto-title — fired when the backend generates an LLM title for the
   * conversation from the first user message (`default_utility` model).
   * The backend persists it; the frontend just updates the store. */
  onConversationTitle?: (title: string) => void;
  /** RAG sources — fired when the agent's rag_search tool retrieves chunks */
  onRagSources?: (sources: RetrievedSourceDTO[], toolCallId: string) => void;
  /** Deliverables — fired when a tool produces a downloadable file (report, presentation) */
  onDeliverables?: (
    deliverables: Array<{
      type: string;
      format: string;
      filename: string;
      file_path: string;
      download_url: string;
      report_id?: string;
      created_at?: number;
    }>,
    toolCallId: string,
  ) => void;
  /** Document digestion progress — fired during chat-upload doc digestion */
  onDocumentDigestProgress?: (p: {
    stage: string;
    percent: number;
    details: string;
    filename?: string;
    document_id?: string | null;
    total_chunks?: number;
    total_images?: number;
  }) => void;
  onDocumentDigestDone?: (doc: {
    filename: string;
    document_id: string;
    total_chunks: number;
    total_images: number;
  }) => void;
  onDocumentDigestError?: (info: { filename?: string; error: string }) => void;
  onDone: () => void;
  onError: (error: string) => void;
}

/**
 * Idle timeout for SSE streams.
 *
 * If no data is received from the server for this duration, the request is
 * considered dead and aborted. This handles cases where the connection is
 * silently dropped or the backend crashes without sending an error event.
 *
 * Set to 10 minutes.
 */
const STREAM_IDLE_TIMEOUT_MS = 10 * 60 * 1000;

/**
 * Absolute maximum duration for a streaming request (safety net).
 *
 * No matter what, the request will be aborted after this duration.
 * Set to 30 minutes — more than enough for even the most complex
 * multi-round agent conversations with long thinking phases.
 */
const STREAM_MAX_TIMEOUT_MS = 30 * 60 * 1000;

const activeControllers = new Set<AbortController>();

/** Stop user-visible text generation without treating it as a timeout. */
export function stopActiveStream(): void {
  for (const controller of activeControllers) {
    controller.abort("user_stop");
  }
  activeControllers.clear();
}

/**
 * Stream chat via the JSON endpoint.
 */
export async function streamChat(
  messages: { role: string; content: string }[],
  model: string,
  callbacks: StreamCallbacks,
  options?: {
    conversationId?: string;
    modelOverride?: string;
    shrug?: boolean;
  },
): Promise<void> {
  const controller = new AbortController();
  activeControllers.add(controller);
  // Safety net: absolute maximum time for the entire request
  const maxTimeoutId = setTimeout(
    () => controller.abort(),
    STREAM_MAX_TIMEOUT_MS,
  );
  // Idle timeout: abort if no data received for this long
  let idleTimeoutId = setTimeout(
    () => controller.abort(),
    STREAM_IDLE_TIMEOUT_MS,
  );

  /** Reset the idle timeout — call every time we receive data from the server */
  const resetIdleTimeout = () => {
    clearTimeout(idleTimeoutId);
    idleTimeoutId = setTimeout(
      () => controller.abort(),
      STREAM_IDLE_TIMEOUT_MS,
    );
  };

  try {
    log(
      `➡️  streamChat START  model=${model}  messages=${messages.length}  convId=${options?.conversationId}`,
    );

    const requestBody = {
      messages,
      model,
      stream: true,
      conversation_id: options?.conversationId,
      model_override: options?.modelOverride,
      shrug: options?.shrug,
    };
    log(
      `   request body: messages=${messages.length}  model=${model}  stream=true  convId=${options?.conversationId ?? "none"}  modelOverride=${options?.modelOverride ?? "none"}`,
    );

    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestBody),
      signal: controller.signal,
      keepalive: true, // allow request to outlive page navigation (for better chance of receiving response in case of accidental nav)
    });

    // We got a response — reset idle timeout
    resetIdleTimeout();

    log(`   response received  status=${response.status}  ok=${response.ok}`);

    if (!response.ok) {
      dbgError(
        `❌ streamChat response NOT OK  status=${response.status}  statusText=${response.statusText}`,
      );
      throw new Error(`HTTP ${response.status}`);
    }

    log("✅ streamChat response OK — starting SSE parse");
    await parseSSEStream(response, callbacks, resetIdleTimeout);
    log("✅ streamChat complete");
  } catch (err) {
    if (controller.signal.reason === "user_stop") {
      callbacks.onDone();
    } else if (err instanceof DOMException && err.name === "AbortError") {
      callbacks.onError(
        "Request timed out. The AI may be loading - please try again.",
      );
    } else {
      const errorMsg = err instanceof Error ? err.message : "Unknown error";
      dbgError(`❌ streamChat error: ${errorMsg}`);
      callbacks.onError(errorMsg);
    }
  } finally {
    clearTimeout(maxTimeoutId);
    clearTimeout(idleTimeoutId);
    activeControllers.delete(controller);
  }
}

/**
 * Stream chat with file uploads (images and documents).
 * Uses the multipart/form-data endpoint.
 */
export async function streamChatWithFiles(
  messages: { role: string; content: string }[],
  model: string,
  callbacks: StreamCallbacks,
  files: {
    images?: File[];
    documents?: File[];
  },
  options?: {
    conversationId?: string;
    modelOverride?: string;
  },
): Promise<void> {
  const controller = new AbortController();
  activeControllers.add(controller);
  const maxTimeoutId = setTimeout(
    () => controller.abort(),
    STREAM_MAX_TIMEOUT_MS,
  );
  let idleTimeoutId = setTimeout(
    () => controller.abort(),
    STREAM_IDLE_TIMEOUT_MS,
  );

  const resetIdleTimeout = () => {
    clearTimeout(idleTimeoutId);
    idleTimeoutId = setTimeout(
      () => controller.abort(),
      STREAM_IDLE_TIMEOUT_MS,
    );
  };

  try {
    log(
      `📤 streamChatWithFiles  images=${files.images?.length ?? 0}  docs=${files.documents?.length ?? 0}`,
    );

    const formData = new FormData();
    formData.append("messages", JSON.stringify(messages));
    if (model) formData.append("model", model);
    if (options?.conversationId)
      formData.append("conversation_id", options.conversationId);
    if (options?.modelOverride)
      formData.append("model_override", options.modelOverride);

    // Attach image files
    if (files.images) {
      for (const img of files.images) {
        formData.append("images", img);
      }
    }

    // Attach document files
    if (files.documents) {
      for (const doc of files.documents) {
        formData.append("documents", doc);
      }
    }

    const response = await fetch("/api/chat/stream/multipart", {
      method: "POST",
      body: formData,
      signal: controller.signal,
      keepalive: true, // allow request to outlive page navigation (for better chance of receiving response in case of accidental nav)
    });

    resetIdleTimeout();

    log(`   response received  status=${response.status}  ok=${response.ok}`);

    if (!response.ok) {
      dbgError(
        `❌ streamChatWithFiles response NOT OK  status=${response.status}`,
      );
      throw new Error(`HTTP ${response.status}`);
    }

    log("✅ streamChatWithFiles response OK — starting SSE parse");
    await parseSSEStream(response, callbacks, resetIdleTimeout);
    log("✅ streamChatWithFiles complete");
  } catch (err) {
    if (controller.signal.reason === "user_stop") {
      callbacks.onDone();
    } else if (err instanceof DOMException && err.name === "AbortError") {
      callbacks.onError(
        "Request timed out. The AI may be loading - please try again.",
      );
    } else {
      const errorMsg = err instanceof Error ? err.message : "Unknown error";
      dbgError(`❌ streamChatWithFiles error: ${errorMsg}`);
      callbacks.onError(errorMsg);
    }
  } finally {
    clearTimeout(maxTimeoutId);
    clearTimeout(idleTimeoutId);
    activeControllers.delete(controller);
  }
}

/**
 * Generic SSE stream parser that handles the backend's event format.
 *
 * @param resetIdleTimeout - Called every time data is received from the server,
 *   to reset the idle timeout and keep the connection alive as long as the
 *   server is still sending data.
 */
async function parseSSEStream(
  response: Response,
  callbacks: StreamCallbacks,
  resetIdleTimeout: () => void,
): Promise<void> {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("No response body");

  const decoder = new TextDecoder();
  let buffer = "";
  let eventCount = 0;

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      log(`   SSE reader done signal (after ${eventCount} events)`);
      break;
    }

    // We received data from the server — reset the idle timeout
    resetIdleTimeout();

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";

    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      const data = line.slice(6).trim();
      if (data === "[DONE]") {
        log("   SSE [DONE] received");
        callbacks.onDone();
        return;
      }

      try {
        const parsed = JSON.parse(data);
        eventCount++;

        // Dispatch the parsed event to the callbacks. Returns true when
        // the event terminates the stream ("done").
        const terminated = dispatchAgentEvent(parsed, callbacks);
        if (terminated) return;
      } catch {
        /* skip malformed JSON */
      }
    }
  }

  // If we reach here, stream ended without [DONE]
  log("   SSE stream ended without [DONE] — calling onDone() manually");
  callbacks.onDone();
}

/**
 * Dispatch ONE parsed agent event to the stream callbacks.
 *
 * This is the single source of truth for the backend agent-event format:
 *   message / thinking_start / thinking / thinking_done /
 *   generation_done / memory_extraction_start / memory_extraction_done /
 *   conversation_title / rag_sources / deliverables /
 *   document_digest_progress / document_digest_done /
 *   document_digest_error / tool_call / done / error.
 *
 * It is used by BOTH transport layers:
 *   • the SSE /api/chat/stream parser above (text chat), and
 *   • the voice WebSocket client, whose `agent_event` frames carry the
 *     exact same event payloads (see voice/useVoiceSession.ts).
 *
 * @returns true if the event terminates the generation ("done").
 */
export function dispatchAgentEvent(
  parsed: unknown,
  callbacks: StreamCallbacks,
): boolean {
  const p = parsed as Record<string, any>;
  const eventType = p.event;

  // ── Message token ──
  if (eventType === "message" && p.message?.content) {
    callbacks.onToken(p.message.content);
  }

  // ── Thinking start ──
  if (eventType === "thinking_start") {
    log("   🧠 thinking_start event received");
    callbacks.onThinkingStart();
  }

  // ── Thinking token ──
  if (eventType === "thinking" && p.thinking) {
    callbacks.onThinkingToken(p.thinking);
  }

  // ── Thinking done ──
  if (eventType === "thinking_done") {
    const dur = p.thinkingDuration;
    log(`   🧠 thinking_done event received  duration=${dur}s`);
    if (dur != null) {
      callbacks.onThinkingDone(dur);
    }
  }

  // ── Generation done (metadata for DB persistence) ──
  if (eventType === "generation_done") {
    log(
      `   ⏱️ generation_done event  thinkingDuration=${p.thinkingDuration}  generationDuration=${p.generationDuration}`,
    );
    callbacks.onGenerationDone({
      thinkingDuration: p.thinkingDuration ?? undefined,
      generationDuration: p.generationDuration ?? 0,
    });
  }

  // ── Memory extraction start ──
  if (eventType === "memory_extraction_start") {
    log("   🧠 memory_extraction_start event received");
    callbacks.onMemoryExtractionStart?.();
  }

  // ── Memory extraction done ──
  if (eventType === "memory_extraction_done") {
    const count = p.count ?? 0;
    const ran = p.ran ?? true;
    const pending = p.pending ?? false;
    log(
      `   🧠 memory_extraction_done event received  count=${count}  ran=${ran}  pending=${pending}`,
    );
    callbacks.onMemoryExtractionDone?.({ count, ran, pending });
  }

  // ── Conversation auto-title (LLM-generated from the first message) ──
  if (eventType === "conversation_title" && p.title) {
    log(`   🏷️ conversation_title event received  title=${p.title}`);
    callbacks.onConversationTitle?.(p.title as string);
  }

  // ── RAG sources (agent's rag_search tool returned chunks) ──
  if (eventType === "rag_sources" && p.sources) {
    const sources = p.sources as RetrievedSourceDTO[];
    const tcId = p.tool_call_id as string | undefined;
    log(
      `   🔎 rag_sources event received  sources=${sources.length}  tool_call_id=${tcId ?? "(none)"}`,
    );
    if (callbacks.onRagSources) {
      callbacks.onRagSources(sources, tcId ?? "");
    }
  }

  // ── Deliverables (reports, presentations) ──
  if (eventType === "deliverables" && p.deliverables) {
    const deliverables = p.deliverables as Array<{
      type: string;
      format: string;
      filename: string;
      file_path: string;
      download_url: string;
      report_id?: string;
      created_at?: number;
    }>;
    const tcId = p.tool_call_id as string | undefined;
    log(
      `   📦 deliverables event received  count=${deliverables.length}  tool_call_id=${tcId ?? "(none)"}`,
    );
    if (callbacks.onDeliverables) {
      callbacks.onDeliverables(deliverables, tcId ?? "");
    }
  }

  // ── Document digestion progress (chat-upload docs) ──
  if (eventType === "document_digest_progress") {
    log(
      `   📄 document_digest_progress  stage=${p.stage}  percent=${p.percent}  file=${p.filename}`,
    );
    callbacks.onDocumentDigestProgress?.({
      stage: p.stage,
      percent: p.percent,
      details: p.details,
      filename: p.filename,
      document_id: p.document_id,
      total_chunks: p.total_chunks,
      total_images: p.total_images,
    });
  }

  if (eventType === "document_digest_done") {
    log(
      `   📄✅ document_digest_done  file=${p.filename}  chunks=${p.total_chunks}`,
    );
    callbacks.onDocumentDigestDone?.({
      filename: p.filename,
      document_id: p.document_id,
      total_chunks: p.total_chunks,
      total_images: p.total_images,
    });
  }

  if (eventType === "document_digest_error") {
    log(`   📄❌ document_digest_error  file=${p.filename}  error=${p.error}`);
    callbacks.onDocumentDigestError?.({
      filename: p.filename,
      error: p.error,
    });
  }

  // ── Tool call event ──
  if (eventType === "tool_call" && p.tool_call) {
    const tc = p.tool_call;
    const tcId = tc.id ?? `tc-${Date.now()}`;

    if (tc.status === "running") {
      log(`   🔧 tool_call running: type=${tc.type} title=${tc.title}`);
      // Build the full ToolCallResult and start a new tool_call block.
      // The backend-provided ID is used directly (no frontend remapping)
      // so subsequent updates and rag_sources events can match by ID.
      callbacks.onToolCallStart({
        id: tcId,
        type: tc.type,
        status: "running",
        title: tc.title ?? tc.type,
        startedAt: Date.now(),
        query: tc.query,
        language: tc.language,
        code: tc.code,
        imageDescription: tc.imageDescription,
        webResults: tc.webResults,
        genResults: tc.genResults,
        output: tc.output,
        exitCode: tc.exitCode,
      });
      // If the backend already sent results in the start event, mark as completed
      if (tc.webResults && tc.webResults.length > 0) {
        callbacks.onToolCallUpdate(tcId, {
          status: "completed",
          completedAt: Date.now(),
          webResults: tc.webResults,
        });
      }
      // Similarly for genResults
      if (tc.genResults && tc.genResults.length > 0) {
        callbacks.onToolCallUpdate(tcId, {
          status: "completed",
          completedAt: Date.now(),
          genResults: tc.genResults,
        });
      }
    } else if (tc.status === "completed" || tc.status === "error") {
      log(`   🔧 tool_call ${tc.status}: type=${tc.type}`);

      const updates: Partial<ToolCallResult> = {
        status: tc.status,
      };
      if (tc.completedAt) updates.completedAt = tc.completedAt;
      if (tc.webResults) updates.webResults = tc.webResults;
      if (tc.genResults) updates.genResults = tc.genResults;
      if (tc.output) updates.output = tc.output;
      if (tc.exitCode !== undefined) updates.exitCode = tc.exitCode;
      if (tc.imageDescription) updates.imageDescription = tc.imageDescription;
      if (tc.error) updates.error = tc.error;

      callbacks.onToolCallUpdate(tcId, updates);
    }
  }

  // ── Done event ──
  if (eventType === "done") {
    log("   done event received");
    callbacks.onDone();
    return true;
  }

  // ── Error event ──
  if (eventType === "error" && p.error) {
    dbgError(`   ❌ error event: ${p.error}`);
    callbacks.onError(
      typeof p.error === "string"
        ? p.error
        : (p.error.message ?? "Unknown error"),
    );
  }

  return false;
}

// ─── Demo Mode ──────────────────────────────────────────────────
// Fallback for when the backend is unreachable.
// Generates mock streaming responses with tool calls.

export async function streamChatDemo(
  userMessage: string,
  callbacks: StreamCallbacks,
): Promise<void> {
  log(`🎭 streamChatDemo  message=${userMessage.slice(0, 80)}`);

  const lowerMsg = userMessage.toLowerCase();

  const hasSearch =
    lowerMsg.includes("search") ||
    lowerMsg.includes("find") ||
    lowerMsg.includes("look up") ||
    lowerMsg.includes("what is") ||
    lowerMsg.includes("who is") ||
    lowerMsg.includes("latest");
  const hasCode =
    lowerMsg.includes("code") ||
    lowerMsg.includes("python") ||
    lowerMsg.includes("script") ||
    lowerMsg.includes("calculate") ||
    lowerMsg.includes("run") ||
    lowerMsg.includes("write a");

  const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

  // Demo: simulate thinking
  callbacks.onThinkingStart();
  const thinkWords = "Let me analyze this question carefully...".split(" ");
  for (let i = 0; i < thinkWords.length; i++) {
    callbacks.onThinkingToken(i === 0 ? thinkWords[i] : " " + thinkWords[i]);
    await delay(40);
  }
  callbacks.onThinkingDone(2);

  if (hasSearch) {
    const tcId = `demo-tc-search-${Date.now()}`;
    callbacks.onToolCallStart({
      id: tcId,
      type: "websearch",
      status: "running",
      title: "Searching the web",
      startedAt: Date.now(),
      query: userMessage,
    });
    await delay(1200);
    callbacks.onToolCallUpdate(tcId, {
      status: "completed",
      completedAt: Date.now(),
      webResults: [
        {
          title: "Wikipedia - Related Topic",
          url: "https://en.wikipedia.org/wiki/Example",
          snippet:
            "Comprehensive overview of the topic with historical context and current developments...",
        },
        {
          title: "Recent Research Paper (2025)",
          url: "https://arxiv.org/abs/example",
          snippet:
            "Novel approach demonstrating significant improvements in accuracy and efficiency...",
        },
        {
          title: "Technical Documentation",
          url: "https://docs.example.com/guide",
          snippet:
            "Official documentation covering installation, configuration, and best practices...",
        },
      ],
    });
  }

  if (hasCode) {
    const codeSnippet = `import numpy as np\n\ndata = np.random.randn(1000)\nmean = np.mean(data)\nstd = np.std(data)\nprint(f"Mean: {mean:.4f}")\nprint(f"Std:  {std:.4f}")`;

    const tcId = `demo-tc-code-${Date.now()}`;
    callbacks.onToolCallStart({
      id: tcId,
      type: "code_exec",
      status: "running",
      title: "Running code",
      startedAt: Date.now(),
      language: "python",
      code: codeSnippet,
    });
    await delay(1500);
    callbacks.onToolCallUpdate(tcId, {
      status: "completed",
      completedAt: Date.now(),
      output: `Mean: 0.0234\nStd:  1.0012\n\nProcess exited with code 0`,
      exitCode: 0,
    });
  }

  const responses: Record<string, string> = {
    default:
      "I'd be happy to help you with that! Let me think about your question and provide a comprehensive answer.\n\nBased on my analysis, here are the key points to consider:\n\n1. **Context matters** — The approach you take should depend on your specific requirements and constraints.\n\n2. **Best practices** — Following established patterns and conventions will help ensure maintainability and reliability.\n\n3. **Testing** — Always verify your assumptions with real-world testing and validation.\n\nWould you like me to elaborate on any of these points?",
    search:
      "I searched the web for you and found several relevant results. Here's a summary of what I found:\n\nThe most up-to-date information suggests that this topic has seen significant developments recently. Key findings include:\n\n- **Recent advances** have improved performance by approximately 30%\n- **New methodologies** are being adopted across the industry\n- **Community consensus** is forming around best practices\n\nCheck the search results in the sandbox panel for more details.",
    code: "I've written and executed the code for you. Here's what happened:\n\nThe code ran successfully and produced the expected output. You can see the full execution details in the sandbox panel.\n\n**Key observations:**\n- The calculation completed without errors\n- Results are statistically significant\n- The approach scales well with larger datasets\n\nWould you like me to modify the code or run additional analysis?",
  };

  let responseKey = "default";
  if (hasSearch && hasCode) responseKey = "search";
  else if (hasSearch) responseKey = "search";
  else if (hasCode) responseKey = "code";

  const response = responses[responseKey];
  const words = response.split(" ");

  for (let i = 0; i < words.length; i++) {
    callbacks.onToken(i === 0 ? words[i] : " " + words[i]);
    await delay(30 + Math.random() * 40);
  }

  log("🎭 streamChatDemo complete");
  callbacks.onGenerationDone({ thinkingDuration: 2, generationDuration: 5 });
  callbacks.onDone();
}
