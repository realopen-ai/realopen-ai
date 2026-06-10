import { useRef, useEffect, useCallback, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { MobileMenuButton } from "@/components/layout/Sidebar";
import { MessageBubble } from "@/components/chat/MessageBubble";
import { InputArea } from "@/components/chat/InputArea";
import { WelcomeScreen } from "@/components/chat/WelcomeScreen";
import { useChatStore, type ToolCallResult } from "@/store/chatStore";
import { useUIStore } from "@/store/uiStore";
import { useSandboxStore } from "@/store/sandboxStore";
import { streamChat, streamChatWithFiles, streamChatDemo } from "@/api/stream";
import { ScrollArea } from "@/components/ui/scroll-area";
import { createDebugLogger } from "@/lib/debug";

const log = createDebugLogger("ChatArea");

const DEMO_MODE = false; // Set to true to enable demo mode with fake streaming responses (for testing without backend)

export function ChatArea() {
  const { conversationId: urlConvId } = useParams<{ conversationId: string }>();
  const navigate = useNavigate();
  const isStreaming = useChatStore((s) => s.isStreaming);
  const setRightPanelOpen = useUIStore((s) => s.setRightPanelOpen);
  const setRightPanelTab = useUIStore((s) => s.setRightPanelTab);
  const addTerminalLine = useSandboxStore((s) => s.addTerminalLine);
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
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, messages.length, messages[messages.length - 1]?.content]);

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
      store.addMessage(convId, {
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

      // Auto-title the conversation based on first user message
      const currentConv = useChatStore
        .getState()
        .conversations.find((c) => c.id === convId);
      if (currentConv && currentConv.messages.length <= 2) {
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
      const capturedGeneration = ++streamGenerationRef.current;

      const callbacks = {
        onToken: (token: string) =>
          useChatStore
            .getState()
            .appendToMessage(capturedConvId, assistantMsgId, token),
        onThinkingStart: () => {
          useChatStore
            .getState()
            .setThinkingState(capturedConvId, assistantMsgId, true);
        },
        onThinkingToken: (token: string) => {
          useChatStore
            .getState()
            .appendToThinking(capturedConvId, assistantMsgId, token);
        },
        onThinkingDone: (durationSeconds: number) => {
          useChatStore
            .getState()
            .setThinkingDuration(
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
          if (data.thinkingDuration != null) {
            s.setThinkingDuration(
              capturedConvId,
              assistantMsgId,
              data.thinkingDuration,
            );
          }
        },
        onToolCallStart: (
          toolCall: Omit<ToolCallResult, "id" | "startedAt">,
        ) => {
          const s = useChatStore.getState();
          const tcId = s.addToolCall(capturedConvId, assistantMsgId, toolCall);
          s.openSandbox(capturedConvId, assistantMsgId);

          if (toolCall.type === "code_exec") {
            setRightPanelOpen(true);
            setRightPanelTab("terminal");
            addTerminalLine(`$ Running ${toolCall.language ?? "code"}...`);
            if (toolCall.code) {
              toolCall.code
                .split("\n")
                .forEach((l) => addTerminalLine(`  ${l}`));
            }
          } else if (toolCall.type === "vision") {
            setRightPanelOpen(true);
            setRightPanelTab("terminal");
            addTerminalLine(`$ Analyzing image...`);
          } else if (toolCall.type === "websearch") {
            addTerminalLine(`$ Searching: ${toolCall.query ?? content}`);
          }

          return tcId;
        },
        onToolCallUpdate: (
          toolCallId: string,
          updates: Partial<ToolCallResult>,
        ) => {
          useChatStore
            .getState()
            .updateToolCall(
              capturedConvId,
              assistantMsgId,
              toolCallId,
              updates,
            );
          if (updates.output) {
            updates.output.split("\n").forEach((l) => addTerminalLine(l));
          }
          if (updates.webResults) {
            addTerminalLine(`  → ${updates.webResults.length} result(s) found`);
          }
          if (updates.genResults) {
            addTerminalLine(
              `  → ${updates.genResults.length} generated result(s)`,
            );
          }
          if (updates.imageDescription) {
            addTerminalLine(
              `  → Image analyzed: ${updates.imageDescription.slice(0, 80)}...`,
            );
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
    ],
  );

  // Listen for regenerate events from MessageBubble
  useEffect(() => {
    const handler = (e: Event) => {
      const { content } = (e as CustomEvent).detail;
      handleSend(content, { modelOverride: undefined, shrug: false });
    };
    window.addEventListener("regenerate-message", handler);
    return () => window.removeEventListener("regenerate-message", handler);
  }, [handleSend]);

  return (
    <div className="flex flex-col h-full bg-background">
      {/* Header */}
      <div className="flex items-center justify-between px-3 pt-3 pb-2.75 border-b border-border/50">
        <div className="flex items-center gap-2">
          <MobileMenuButton />
          <h2 className="text-[14px] font-medium text-foreground truncate">
            {conv?.title ?? "New Chat"}
          </h2>
        </div>
      </div>

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
            {messages.map((msg) => (
              <MessageBubble
                key={msg.id}
                message={msg}
                conversationId={effectiveConvId!}
              />
            ))}
            <div ref={messagesEndRef} />
          </div>
        </ScrollArea>
      )}

      <InputArea onSend={handleSend} isStreaming={isStreaming} />
    </div>
  );
}
