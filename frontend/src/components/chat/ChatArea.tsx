import { useRef, useEffect, useCallback, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { MobileMenuButton } from "@/components/layout/Sidebar";
import { MessageBubble } from "@/components/chat/MessageBubble";
import { InputArea } from "@/components/chat/InputArea";
import { VoiceCallPanel } from "@/components/chat/VoiceCallPanel";
import { WelcomeScreen } from "@/components/chat/WelcomeScreen";
import {
  useChatStore,
  type ToolCallResult,
  type Deliverable as DelivType,
} from "@/store/chatStore";
import { useUIStore } from "@/store/uiStore";
import { useSandboxStore } from "@/store/sandboxStore";
import { useMemoryStore } from "@/store/memoryStore";
import { useT } from "@/store/settingsStore";
import { Brain, Check, Mic } from "lucide-react";
import {
  streamChat,
  streamChatWithFiles,
  streamChatDemo,
  stopActiveStream,
  type StreamCallbacks,
} from "@/api/stream";
import type { RetrievedSourceDTO } from "@/api/documentsClient";
import { ScrollArea } from "@/components/ui/scroll-area";
import { createDebugLogger } from "@/lib/debug";
import {
  useVoiceSession,
  type BuildStreamCallbacksOptions,
} from "@/voice/useVoiceSession";
import { useVoiceStore, INTERRUPT_FLASH_MS } from "@/voice/voiceStore";
import { responseTransportForStop } from "@/voice/responseControl";
import { cn } from "@/lib/utils";
import { formatCodeExecOutput } from "@/lib/codeExecOutput";

const log = createDebugLogger("ChatArea");

const DEMO_MODE = false; // Set to true to enable demo mode with fake streaming responses (for testing without backend)

export function ChatArea() {
  const { conversationId: urlConvId } = useParams<{ conversationId: string }>();
  const navigate = useNavigate();
  const isStreaming = useChatStore((s) => s.isStreaming);
  const setRightPanelOpen = useUIStore((s) => s.setRightPanelOpen);
  const setRightPanelTab = useUIStore((s) => s.setRightPanelTab);
  const addTerminalLine = useSandboxStore((s) => s.addTerminalLine);
  const setPreviewUrl = useSandboxStore((s) => s.setPreviewUrl);
  const isExtracting = useMemoryStore((s) => s.isExtracting);
  const lastExtraction = useMemoryStore((s) => s.lastExtraction);
  const t = useT();
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // When a new conversation is created from the home page, we keep the
  // conversation ID here so we can display messages before the URL changes.
  // After the stream finishes, we navigate to /{convId} seamlessly.
  const [pendingConvId, setPendingConvId] = useState<string | null>(null);

  // Loading state for when fetching a conversation from the backend by URL
  const [isLoadingConv, setIsLoadingConv] = useState(false);

  // Stream generation counter — prevents stale onDone/onError callbacks
  // from redirecting the user after they've navigated away.
  const streamGenerationRef = useRef(0);

  // The effective conversation ID: URL param takes priority, then pending
  const effectiveConvId = urlConvId ?? pendingConvId ?? null;

  const conv = useChatStore((s) =>
    effectiveConvId
      ? s.conversations.find((c) => c.id === effectiveConvId)
      : undefined,
  );
  const messages = conv?.messages ?? [];

  // ── Voice session state (reactive, for indicators) ──
  const voiceState = useVoiceStore((s) => s.voiceState);
  const partialTranscript = useVoiceStore((s) => s.partialTranscript);
  const interruptFlashAt = useVoiceStore((s) => s.interruptFlashAt);
  const [flashActive, setFlashActive] = useState(false);
  useEffect(() => {
    if (interruptFlashAt == null) {
      setFlashActive(false);
      return;
    }
    setFlashActive(true);
    const timer = setTimeout(
      () => setFlashActive(false),
      Math.max(0, INTERRUPT_FLASH_MS - (Date.now() - interruptFlashAt)),
    );
    return () => clearTimeout(timer);
  }, [interruptFlashAt]);

  // ── Sync URL conversation ID with store on mount / URL change ──
  useEffect(() => {
    if (!urlConvId) {
      // On home page — clear active conversation (unless there's a pending one)
      if (!pendingConvId) {
        useChatStore.getState().setActiveConversation(null);
      }
      return;
    }

    // Check if conversation exists in the store
    const existsInStore = useChatStore
      .getState()
      .conversations.find((c) => c.id === urlConvId);

    if (existsInStore) {
      // Already in store — just set as active and load messages if needed
      useChatStore.getState().setActiveConversation(urlConvId);
      return;
    }

    // Not in store — try to load from backend
    setIsLoadingConv(true);
    useChatStore
      .getState()
      .loadConversationById(urlConvId)
      .then((found) => {
        setIsLoadingConv(false);
        if (!found) {
          log("Conversation %s not found, redirecting to home", urlConvId);
          navigate("/", { replace: true });
        }
      });
  }, [urlConvId, pendingConvId, navigate]);

  // ── Clear pending state when URL catches up ──
  useEffect(() => {
    if (urlConvId && pendingConvId && urlConvId === pendingConvId) {
      // URL now matches the pending conversation — clean up
      setPendingConvId(null);
      useChatStore.getState().setActiveConversation(urlConvId);
    }
  }, [urlConvId, pendingConvId]);

  // ── Increment stream generation when navigating away ──
  useEffect(() => {
    // When the URL changes, increment the stream generation so that
    // any stale onDone/onError callbacks don't redirect the user
    streamGenerationRef.current += 1;
  }, [urlConvId]);

  // ── Scroll to bottom on new messages ──
  useEffect(() => {
    // wait 100ms to allow the new message to render before scrolling
    const timeout = setTimeout(() => {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }, 100);
    return () => clearTimeout(timeout);
  }, [messages, messages.length, messages[messages.length - 1]?.content]);

  // ── Shared stream callbacks (used by BOTH text chat and voice) ──
  // Extracted from handleSend so the voice pipeline (useVoiceSession) can
  // drive the EXACT same store updates — voice responses render identically
  // to text responses (thinking blocks, text tokens, tool_call blocks,
  // rag sources, deliverables, generation duration, navigation).
  const buildStreamCallbacks = useCallback(
    (
      convId: string,
      assistantMsgId: string,
      opts: BuildStreamCallbacksOptions,
    ): StreamCallbacks => {
      const { userMsgId, isFromHomePage } = opts;

      // Capture values for the closure — these won't change after this point
      const capturedConvId = convId;
      const capturedIsFromHomePage = isFromHomePage;
      const capturedGeneration = ++streamGenerationRef.current;
      const terminalToolIds = new Set<string>();
      const streamedToolTypes = new Map<string, ToolCallResult["type"]>();

      return {
        onToken: (token: string) =>
          useChatStore
            .getState()
            .appendTextToken(capturedConvId, assistantMsgId, token),
        onThinkingStart: () => {
          useChatStore
            .getState()
            .startThinkingBlock(capturedConvId, assistantMsgId);
        },
        onThinkingToken: (token: string) => {
          useChatStore
            .getState()
            .appendThinkingToken(capturedConvId, assistantMsgId, token);
        },
        onThinkingDone: (durationSeconds: number) => {
          useChatStore
            .getState()
            .finishThinkingBlock(
              capturedConvId,
              assistantMsgId,
              durationSeconds,
            );
        },
        onGenerationDone: (data: {
          thinkingDuration?: number;
          generationDuration: number;
        }) => {
          const s = useChatStore.getState();
          s.setGenerationDuration(
            capturedConvId,
            assistantMsgId,
            data.generationDuration,
          );
          // Re-enable the input as soon as generation completes — the
          // memory extraction step (which runs after this event but before
          // [DONE]) is non-blocking and shouldn't keep the input disabled.
          s.setStreaming(capturedConvId, assistantMsgId, false);
        },
        onMemoryExtractionStart: () => {
          useMemoryStore.getState().setExtracting(true);
        },
        // Auto-title — the backend generated an LLM title from the first
        // user message (default_utility model) and already persisted it;
        // update the sidebar live. Replaces the optimistic preview above.
        onConversationTitle: (title: string) => {
          useChatStore.getState().setConversationTitle(capturedConvId, title);
        },
        onMemoryExtractionDone: ({
          count,
          ran,
          pending,
        }: {
          count: number;
          ran: boolean;
          pending?: boolean;
        }) => {
          const ms = useMemoryStore.getState();
          ms.setExtracting(false);
          if (ran && count > 0) {
            ms.setLastExtraction(count);
            // Refresh the Brain page memory list so newly-extracted
            // memories appear if the user navigates there.
            ms.loadMemories();
            ms.loadCategories();
          } else if (ran && pending) {
            // Extraction was enqueued to the background queue (KV-cache
            // protection). The actual count isn't available yet — refresh
            // the memories list after a short delay so the user sees the
            // new memories once the background job completes.
            ms.clearLastExtraction();
            setTimeout(() => {
              ms.loadMemories();
              ms.loadCategories();
            }, 5000);
          } else {
            ms.clearLastExtraction();
          }
        },
        // RAG sources — attach to the tool_call block by tool call ID.
        onRagSources: (sources: RetrievedSourceDTO[], toolCallId: string) => {
          useChatStore
            .getState()
            .setToolCallSources(
              capturedConvId,
              assistantMsgId,
              toolCallId,
              sources,
            );
        },
        // Deliverables — add to the message's deliverables array so the
        // download badges show up immediately (without page refresh).
        onDeliverables: (
          deliverables: Array<{
            type: string;
            format: string;
            filename: string;
            file_path: string;
            download_url: string;
            report_id?: string;
            created_at?: number;
          }>,
          _toolCallId: string,
        ) => {
          useChatStore
            .getState()
            .addDeliverables(
              capturedConvId,
              assistantMsgId,
              deliverables as DelivType[],
            );
        },
        // Document digestion progress — emitted while chat-uploaded
        // documents are being extracted/chunked/embedded. Surface it
        // BOTH in the right panel terminal AND inline on the user's
        // message bubble so the user sees real-time progress in the
        // conversation itself (not just in a side panel).
        onDocumentDigestProgress: (p: {
          stage: string;
          percent: number;
          details: string;
          filename?: string;
          document_id?: string | null;
          total_chunks?: number;
          total_images?: number;
        }) => {
          // Inline progress on the user's message bubble
          // so the user sees feedback directly in the conversation.
          // (Voice turns never upload documents → userMsgId is undefined.)
          if (userMsgId) {
            useChatStore
              .getState()
              .addDigestProgress(capturedConvId, userMsgId, {
                filename: p.filename ?? "document",
                stage: p.stage as
                  | "started"
                  | "extracting_text"
                  | "extracting_images"
                  | "chunking"
                  | "describing_images"
                  | "embedding"
                  | "persisting"
                  | "done"
                  | "error",
                percent: p.percent,
                details: p.details,
                documentId: p.document_id,
                totalChunks: p.total_chunks,
                totalImages: p.total_images,
              });
          }
        },
        onDocumentDigestDone: (doc: {
          filename: string;
          total_chunks: number;
          total_images: number;
        }) => {
          // Final "done" item so the inline indicator shows completion
          if (userMsgId) {
            useChatStore
              .getState()
              .addDigestProgress(capturedConvId, userMsgId, {
                filename: doc.filename,
                stage: "done",
                percent: 100,
                details: `${doc.total_chunks} chunks, ${doc.total_images} image(s)`,
                totalChunks: doc.total_chunks,
                totalImages: doc.total_images,
              });
          }
        },
        onDocumentDigestError: (info: { filename?: string; error: string }) => {
          if (userMsgId) {
            useChatStore
              .getState()
              .addDigestProgress(capturedConvId, userMsgId, {
                filename: info.filename ?? "document",
                stage: "error",
                percent: 100,
                details: info.error,
                error: info.error,
              });
          }
        },
        onToolCallStart: (toolCall: ToolCallResult) => {
          const s = useChatStore.getState();
          s.startToolCallBlock(capturedConvId, assistantMsgId, toolCall);
          streamedToolTypes.set(toolCall.id, toolCall.type);

          if (
            toolCall.sandboxId &&
            useSandboxStore.getState().sandboxId !== toolCall.sandboxId
          ) {
            void useSandboxStore.getState().activateSandbox(toolCall.sandboxId);
          }

          if (toolCall.type === "code_exec") {
            if (!toolCall.sandboxId && !useSandboxStore.getState().sandboxId) {
              void useSandboxStore
                .getState()
                .loadForConversation(capturedConvId);
            }
            terminalToolIds.add(toolCall.id);
            setRightPanelOpen(true);
            setRightPanelTab("code");
            if (toolCall.language === "shell" && toolCall.code) {
              addTerminalLine(`$ ${toolCall.code}`);
            } else {
              addTerminalLine(`$ Running ${toolCall.language ?? "code"}...`);
            }
            if (toolCall.code) {
              if (toolCall.language !== "shell") {
                toolCall.code
                  .split("\n")
                  .forEach((l) => addTerminalLine(`  ${l}`));
              }
            }
          } else if (toolCall.type === "preview") {
            setRightPanelOpen(true);
            setRightPanelTab("preview");
          } else if (toolCall.type === "sandbox") {
            setRightPanelOpen(true);
            setRightPanelTab("code");
          }
        },
        onToolCallUpdate: (
          toolCallId: string,
          updates: Partial<ToolCallResult>,
        ) => {
          useChatStore
            .getState()
            .updateToolCallBlock(
              capturedConvId,
              assistantMsgId,
              toolCallId,
              updates,
            );
          if (
            updates.sandboxId &&
            useSandboxStore.getState().sandboxId !== updates.sandboxId
          ) {
            void useSandboxStore.getState().activateSandbox(updates.sandboxId);
          }
          if (terminalToolIds.has(toolCallId) && updates.output) {
            const output = formatCodeExecOutput(updates.output);
            if (output) addTerminalLine(output);
          }
          if (updates.previewUrl) {
            setPreviewUrl(updates.previewUrl);
            setRightPanelOpen(true);
            setRightPanelTab("preview");
          }
          if (
            updates.status === "completed" &&
            streamedToolTypes.get(toolCallId) === "file_write"
          ) {
            void useSandboxStore.getState().fetchFileTree();
          }
          if (updates.status === "completed" || updates.status === "error") {
            terminalToolIds.delete(toolCallId);
            streamedToolTypes.delete(toolCallId);
          }
        },
        onDone: () => {
          useChatStore
            .getState()
            .setStreaming(capturedConvId, assistantMsgId, false);
          // Only redirect if the stream generation matches (user hasn't navigated away)
          // and this conversation was created from the home page.
          if (
            capturedIsFromHomePage &&
            streamGenerationRef.current === capturedGeneration
          ) {
            log("Stream done — navigating to /%s (replace)", capturedConvId);
            navigate(`/${capturedConvId}`, { replace: true });
          }
        },
        onError: (error: string) => {
          useChatStore
            .getState()
            .updateMessage(capturedConvId, assistantMsgId, {
              content: `Sorry, I encountered an error: ${error}\n\nPlease make sure Ollama is running.`,
            });
          useChatStore
            .getState()
            .setStreaming(capturedConvId, assistantMsgId, false);
          // Still redirect even on error so the URL reflects the conversation
          // (only if the user hasn't navigated away)
          if (
            capturedIsFromHomePage &&
            streamGenerationRef.current === capturedGeneration
          ) {
            navigate(`/${capturedConvId}`, { replace: true });
          }
        },
      };
    },
    [
      navigate,
      addTerminalLine,
      setPreviewUrl,
      setRightPanelOpen,
      setRightPanelTab,
    ],
  );

  // ── Handle sending a message ──
  const handleSend = useCallback(
    async (
      content: string,
      options?: {
        modelOverride?: string;
        shrug?: boolean;
        images?: File[];
        documents?: File[];
      },
    ) => {
      const store = useChatStore.getState();

      // Determine which conversation to use
      let convId = effectiveConvId;
      const isFromHomePage = !urlConvId && !pendingConvId;

      if (!convId) {
        log("No active conversation — creating one...");
        convId = await store.createConversation();
        log(`Created conversation: ${convId}`);

        // Track this as a pending conversation (created from home page)
        // We'll redirect after streaming completes
        if (isFromHomePage) {
          setPendingConvId(convId);
        }
      }

      log(
        `handleSend  content=${content.slice(0, 60)}  modelOverride=${options?.modelOverride}  images=${options?.images?.length ?? 0}  docs=${options?.documents?.length ?? 0}`,
      );

      // Use model override if provided, otherwise use selected model
      const modelForMessage = options?.modelOverride ?? store.selectedModel;

      // Build messages array for the API BEFORE adding new messages to the store.
      const convBeforeSend = useChatStore
        .getState()
        .conversations.find((c) => c.id === convId);
      const allMessages = [
        ...(convBeforeSend?.messages ?? []).map((m) => ({
          role: m.role,
          content: m.content,
        })),
        { role: "user" as const, content },
      ];

      // Now add the user and assistant messages to the store for UI display
      // Capture the user message ID so we can attach document digestion
      // progress events to it later (real-time inline progress indicator).
      const userMsgId = store.addMessage(convId, {
        role: "user",
        content,
        shrugOverlay: options?.shrug,
        hasImage: !!(options?.images && options.images.length > 0),
        hasDocument: !!(options?.documents && options.documents.length > 0),
        imageCount: options?.images?.length ?? 0,
        documentCount: options?.documents?.length ?? 0,
      });
      const assistantMsgId = store.addMessage(convId, {
        role: "assistant",
        content: "",
        model: modelForMessage,
      });
      store.setStreaming(convId, assistantMsgId, true);

      // Optimistic preview title from the first user message — shows
      // instantly in the sidebar while the backend generates the real LLM
      // title (the `conversation_title` SSE event replaces this preview
      // once it arrives).
      const currentConv = useChatStore
        .getState()
        .conversations.find((c) => c.id === convId);
      if (
        currentConv &&
        currentConv.messages.length <= 2 &&
        (!currentConv.title || currentConv.title === "New Chat")
      ) {
        const title =
          content.length > 40 ? content.slice(0, 40) + "..." : content;
        useChatStore.setState((s) => ({
          conversations: s.conversations.map((c) =>
            c.id === convId ? { ...c, title } : c,
          ),
        }));
      }

      // Capture values for the closure — these won't change after this point
      const capturedConvId = convId;
      const capturedIsFromHomePage = isFromHomePage;

      // Shared callbacks (identical behavior to the pre-voice inline
      // object — the voice pipeline reuses the same function).
      const callbacks = buildStreamCallbacks(capturedConvId, assistantMsgId, {
        userMsgId,
        content,
        isFromHomePage: capturedIsFromHomePage,
      });

      const streamOptions = {
        conversationId: capturedConvId,
        modelOverride: options?.modelOverride,
        shrug: options?.shrug,
      };

      try {
        // If there are file attachments, use the multipart endpoint
        if (
          (options?.images && options.images.length > 0) ||
          (options?.documents && options.documents.length > 0)
        ) {
          log("Calling streamChatWithFiles...");
          await streamChatWithFiles(
            allMessages,
            modelForMessage,
            callbacks,
            {
              images: options.images,
              documents: options.documents,
            },
            streamOptions,
          );
        } else {
          // Use the regular JSON streaming endpoint
          log(
            `Calling streamChat  model=${modelForMessage}  convId=${capturedConvId}  messages=${allMessages.length}`,
          );
          await streamChat(
            allMessages,
            modelForMessage,
            callbacks,
            streamOptions,
          );
        }
      } catch {
        if (DEMO_MODE) {
          // Fallback to demo mode if backend is unreachable
          await streamChatDemo(content, callbacks);
        } else {
          // streamChat / streamChatWithFiles already handle errors via onError callback.
          // Only use demo as last resort if the assistant message is still empty.
          const currentMsg = useChatStore
            .getState()
            .conversations.find((c) => c.id === capturedConvId)
            ?.messages.find((m) => m.id === assistantMsgId);
          if (!currentMsg?.content) {
            await streamChatDemo(content, callbacks);
          }
        }
      }
    },
    [
      effectiveConvId,
      urlConvId,
      pendingConvId,
      navigate,
      addTerminalLine,
      setRightPanelOpen,
      setRightPanelTab,
      buildStreamCallbacks,
    ],
  );

  // ── Voice session (one per ChatArea; drives the same store) ──
  const voiceSession = useVoiceSession({
    conversationId: effectiveConvId,
    buildStreamCallbacks,
    onConversationCreated: (convId) => setPendingConvId(convId),
  });

  const handleStopResponse = useCallback(() => {
    if (responseTransportForStop(voiceState) === "voice") {
      voiceSession.interruptSpeaking();
      return;
    }
    stopActiveStream();
  }, [voiceState, voiceSession]);

  // Listen for regenerate events from MessageBubble
  useEffect(() => {
    const handler = (e: Event) => {
      const { content } = (e as CustomEvent).detail;
      handleSend(content, { modelOverride: undefined, shrug: false });
    };
    window.addEventListener("regenerate-message", handler);
    return () => window.removeEventListener("regenerate-message", handler);
  }, [handleSend]);

  // Partial-transcript ghost bubble — visible while the user is speaking
  // (listening/processing states), replaced by the persisted user message
  // on `user_message`.
  const showPartialBubble =
    (voiceState === "listening" || voiceState === "processing") &&
    partialTranscript.length > 0;
  return (
    <div className="relative flex flex-col h-full overflow-hidden bg-background">
      {/* Header */}
      <div className="flex items-center justify-between px-3 pt-3 pb-2.75 border-b border-border/50">
        <div className="flex items-center gap-2">
          <MobileMenuButton />
          <h2 className="text-[14px] font-medium text-foreground truncate">
            {conv?.title ?? "New Chat"}
          </h2>
        </div>
      </div>

      <VoiceCallPanel
        onToggleMute={voiceSession.toggleVoice}
        onEnd={voiceSession.stopVoice}
        onStopResponse={handleStopResponse}
      />

      {/* Messages or Welcome */}
      {isLoadingConv ? (
        <div className="flex-1 flex items-center justify-center">
          <div className="flex items-center gap-2 text-muted-foreground/50">
            <div className="w-4 h-4 border-2 border-primary border-t-transparent rounded-full animate-spin" />
            <span className="text-[12px]">Loading conversation...</span>
          </div>
        </div>
      ) : messages.length === 0 && !effectiveConvId ? (
        <WelcomeScreen onSend={handleSend} />
      ) : messages.length === 0 ? (
        <ScrollArea className="flex-1">
          <div className="max-w-3xl mx-auto px-4 py-6 space-y-6" />
        </ScrollArea>
      ) : (
        <ScrollArea className="flex-1">
          <div className="max-w-3xl mx-auto px-4 py-6 space-y-6">
            {messages.map((msg, i) => {
              // Briefly flash the last assistant bubble when the user
              // interrupted the assistant (barge-in feedback).
              const isFlashTarget =
                flashActive &&
                msg.role === "assistant" &&
                i === messages.length - 1;
              return (
                <div
                  key={msg.id}
                  className={cn(
                    isFlashTarget && "voice-interrupt-flash rounded-2xl",
                  )}
                >
                  <MessageBubble
                    message={msg}
                    conversationId={effectiveConvId!}
                  />
                </div>
              );
            })}
            <div ref={messagesEndRef} />
          </div>
        </ScrollArea>
      )}

      {/* Memory extraction indicator — appears below the latest assistant
          message while extraction runs and for a few seconds after it
          completes, then fades out. The input is re-enabled on
          generation_done so the user can keep typing while extraction
          runs in the background. */}
      {(isExtracting || lastExtraction) && (
        <MemoryExtractionIndicator
          isExtracting={isExtracting}
          lastExtraction={lastExtraction}
          onDismiss={() => useMemoryStore.getState().clearLastExtraction()}
          t={t}
        />
      )}

      {/* Partial transcript ghost bubble — transient user-style bubble
          while the user speaks; replaced by the persisted user message. */}
      {showPartialBubble && (
        <div className="px-4 pb-1.5 animate-fade-in">
          <div className="max-w-3xl mx-auto flex justify-end">
            <div
              className="max-w-[85%] md:max-w-[75%] rounded-2xl bg-primary/85 text-primary-foreground px-4 py-2.5 flex items-start gap-2"
              aria-live="polite"
            >
              <Mic className="w-3.5 h-3.5 mt-0.5 shrink-0 opacity-80" />
              <p
                className="whitespace-pre-wrap leading-relaxed text-[13px]"
                style={{ fontSize: "var(--app-font-size)" }}
              >
                {partialTranscript}
                <span className="voice-typing-dots" aria-hidden="true">
                  <span />
                  <span />
                  <span />
                </span>
              </p>
            </div>
          </div>
        </div>
      )}

      <InputArea
        onSend={handleSend}
        isStreaming={isStreaming}
        onStartVoice={voiceSession.toggleVoice}
        onStopResponse={handleStopResponse}
      />
    </div>
  );
}

// ─── Memory extraction indicator ──────────────────────────────────────

function MemoryExtractionIndicator({
  isExtracting,
  lastExtraction,
  onDismiss,
  t,
}: {
  isExtracting: boolean;
  lastExtraction: { count: number; timestamp: number } | null;
  onDismiss: () => void;
  t: (key: string, params?: Record<string, string | number>) => string;
}) {
  // Auto-dismiss the "✓ done" state after 4 seconds
  useEffect(() => {
    if (!isExtracting && lastExtraction) {
      const timer = setTimeout(onDismiss, 4000);
      return () => clearTimeout(timer);
    }
  }, [isExtracting, lastExtraction, onDismiss]);

  if (isExtracting) {
    return (
      <div className="flex items-center gap-2 px-4 py-1.5 bg-primary/5 border-t border-primary/10 animate-in fade-in slide-in-from-bottom-1 duration-200">
        <Brain className="w-3 h-3 text-primary animate-pulse" />
        <span className="text-[11px] text-primary/70">
          {t("brain.memories.extracting")}
        </span>
      </div>
    );
  }

  if (lastExtraction) {
    const count = lastExtraction.count;
    return (
      <div className="flex items-center gap-2 px-4 py-1.5 bg-emerald-500/5 border-t border-emerald-500/10 animate-in fade-in slide-in-from-bottom-1 duration-200">
        <Check className="w-3 h-3 text-emerald-500" />
        <span className="text-[11px] text-emerald-600 dark:text-emerald-400">
          {count === 1
            ? t("brain.memories.extracted")
            : t("brain.memories.extractedN", { count })}
        </span>
      </div>
    );
  }

  return null;
}
