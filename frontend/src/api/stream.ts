import type { ToolCallResult } from "@/store/chatStore";
import { dbgError, isDebug, createDebugLogger } from "@/lib/debug";

const log = createDebugLogger("stream");

export interface StreamCallbacks {
  onToken: (token: string) => void;
  onToolCallStart: (
    toolCall: Omit<ToolCallResult, "id" | "startedAt">,
  ) => string;
  onToolCallUpdate: (
    toolCallId: string,
    updates: Partial<ToolCallResult>,
  ) => void;
  onDone: () => void;
  onError: (error: string) => void;
}

/** Default timeout for streaming requests (5 minutes - LLM generation can be slow) */
const STREAM_TIMEOUT_MS = 5 * 60 * 1000;

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
  const timeoutId = setTimeout(() => controller.abort(), STREAM_TIMEOUT_MS);

  try {
    log(
      "➡️  streamChat START  model=%s  messages=%d  convId=%s",
      model,
      messages.length,
      options?.conversationId,
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
      "   request body: messages=%d  model=%s  stream=true  convId=%s  modelOverride=%s",
      messages.length,
      model,
      options?.conversationId ?? "none",
      options?.modelOverride ?? "none",
    );

    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestBody),
      signal: controller.signal,
    });

    log(`   response received  status=${response.status}  ok=${response.ok}`);

    if (!response.ok) {
      dbgError(
        `❌ streamChat response NOT OK  status=${response.status}  statusText=${response.statusText}`,
      );
      throw new Error(`HTTP ${response.status}`);
    }

    log("✅ streamChat response OK — starting SSE parse");
    await parseSSEStream(response, callbacks);
    log("✅ streamChat complete");
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      callbacks.onError(
        "Request timed out. The AI may be loading - please try again.",
      );
    } else {
      const errorMsg = err instanceof Error ? err.message : "Unknown error";
      dbgError(`❌ streamChat error: ${errorMsg}`);
      callbacks.onError(errorMsg);
    }
  } finally {
    clearTimeout(timeoutId);
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
  const timeoutId = setTimeout(() => controller.abort(), STREAM_TIMEOUT_MS);

  try {
    log(
      "📤 streamChatWithFiles  images=%d  docs=%d",
      files.images?.length ?? 0,
      files.documents?.length ?? 0,
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
    });

    log("   response received  status=%d  ok=%s", response.status, response.ok);

    if (!response.ok) {
      dbgError(
        `❌ streamChatWithFiles response NOT OK  status=${response.status}`,
      );
      throw new Error(`HTTP ${response.status}`);
    }

    log("✅ streamChatWithFiles response OK — starting SSE parse");
    await parseSSEStream(response, callbacks);
    log("✅ streamChatWithFiles complete");
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      callbacks.onError(
        "Request timed out. The AI may be loading - please try again.",
      );
    } else {
      const errorMsg = err instanceof Error ? err.message : "Unknown error";
      dbgError(`❌ streamChatWithFiles error: ${errorMsg}`);
      callbacks.onError(errorMsg);
    }
  } finally {
    clearTimeout(timeoutId);
  }
}

/**
 * Generic SSE stream parser that handles the backend's event format.
 */
async function parseSSEStream(
  response: Response,
  callbacks: StreamCallbacks,
): Promise<void> {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("No response body");

  const decoder = new TextDecoder();
  let buffer = "";
  let eventCount = 0;

  // Track tool calls by their backend-provided IDs so updates match
  const toolCallIdMap = new Map<string, string>();

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      log(`   SSE reader done signal (after ${eventCount} events)`);
      break;
    }

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
        const eventType = parsed.event;
        eventCount++;

        if (isDebug() && eventCount <= 10) {
          log(`   SSE event #${eventCount}: type=${eventType}`);
        }

        // ── Message token ──
        if (eventType === "message" && parsed.message?.content) {
          callbacks.onToken(parsed.message.content);
        }

        // ── Tool call event ──
        if (eventType === "tool_call" && parsed.tool_call) {
          const tc = parsed.tool_call;

          if (tc.status === "running") {
            log(`   🔧 tool_call running: type=${tc.type} title=${tc.title}`);
            const frontendId = callbacks.onToolCallStart({
              type: tc.type,
              status: "running",
              title: tc.title ?? tc.type,
              query: tc.query,
              language: tc.language,
              code: tc.code,
              imageDescription: tc.image_description,
              results: tc.results,
              output: tc.output,
              exitCode: tc.exitCode,
            });
            // Map backend ID → frontend ID for future updates
            if (tc.id) {
              toolCallIdMap.set(tc.id, frontendId);
            }
            // If the backend already sent results in the start event, mark as completed
            if (tc.results && tc.results.length > 0) {
              callbacks.onToolCallUpdate(frontendId, {
                status: "completed",
                completedAt: Date.now(),
                results: tc.results,
              });
            }
          } else if (tc.status === "completed" || tc.status === "error") {
            log(`   🔧 tool_call ${tc.status}: type=${tc.type}`);
            const frontendId = tc.id ? toolCallIdMap.get(tc.id) : undefined;

            const updates: Partial<ToolCallResult> = {
              status: tc.status,
            };
            if (tc.completedAt) updates.completedAt = tc.completedAt;
            if (tc.results) updates.results = tc.results;
            if (tc.output) updates.output = tc.output;
            if (tc.exitCode !== undefined) updates.exitCode = tc.exitCode;
            if (tc.image_description)
              updates.imageDescription = tc.image_description;
            if (tc.error) updates.error = tc.error;

            if (frontendId) {
              callbacks.onToolCallUpdate(frontendId, updates);
            }
          }
        }

        // ── Done event ──
        if (eventType === "done") {
          log("   SSE done event received");
          callbacks.onDone();
          return;
        }

        // ── Error event ──
        if (eventType === "error" && parsed.error) {
          dbgError(`   ❌ SSE error event: ${parsed.error}`);
          callbacks.onError(
            typeof parsed.error === "string"
              ? parsed.error
              : (parsed.error.message ?? "Unknown error"),
          );
        }
      } catch {
        /* skip malformed JSON */
      }
    }
  }

  // If we reach here, stream ended without [DONE]
  log("   SSE stream ended without [DONE] — calling onDone() manually");
  callbacks.onDone();
}

// ─── Demo Mode ──────────────────────────────────────────────────
// Fallback for when the backend is unreachable.
// Generates mock streaming responses with tool calls.

export async function streamChatDemo(
  userMessage: string,
  callbacks: StreamCallbacks,
): Promise<void> {
  log("🎭 streamChatDemo  message=%s", userMessage.slice(0, 80));

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

  if (hasSearch) {
    const tcId = callbacks.onToolCallStart({
      type: "websearch",
      status: "running",
      title: "Searching the web",
      query: userMessage,
    });
    await delay(1200);
    callbacks.onToolCallUpdate(tcId, {
      status: "completed",
      completedAt: Date.now(),
      results: [
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

    const tcId = callbacks.onToolCallStart({
      type: "code_exec",
      status: "running",
      title: "Running code",
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
  callbacks.onDone();
}
