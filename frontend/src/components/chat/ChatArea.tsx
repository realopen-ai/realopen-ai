import { useRef, useEffect, useCallback } from "react";
import { MobileMenuButton } from "@/components/layout/Sidebar";
import { MessageBubble } from "@/components/chat/MessageBubble";
import { InputArea } from "@/components/chat/InputArea";
import { WelcomeScreen } from "@/components/chat/WelcomeScreen";
import { useChatStore } from "@/store/chatStore";
import { useUIStore } from "@/store/uiStore";
import { useSandboxStore } from "@/store/sandboxStore";
import { streamChat, streamChatDemo } from "@/api/stream";
import { ScrollArea } from "@/components/ui/scroll-area";

export function ChatArea() {
  const store = useChatStore();
  const activeConversationId = useChatStore((s) => s.activeConversationId);
  const isStreaming = useChatStore((s) => s.isStreaming);
  const setRightPanelOpen = useUIStore((s) => s.setRightPanelOpen);
  const setRightPanelTab = useUIStore((s) => s.setRightPanelTab);
  const addTerminalLine = useSandboxStore((s) => s.addTerminalLine);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const conv = store.getActiveConversation();
  const messages = conv?.messages ?? [];

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, messages.length, messages[messages.length - 1]?.content]);

  const handleSend = useCallback(
    async (
      content: string,
      options?: { modelOverride?: string; shrug?: boolean },
    ) => {
      let convId = activeConversationId;
      if (!convId) convId = store.createConversation();

      // Use model override if provided, otherwise use selected model
      const modelForMessage = options?.modelOverride ?? store.selectedModel;

      store.addMessage(convId, {
        role: "user",
        content,
        shrugOverlay: options?.shrug,
      });
      const assistantMsgId = store.addMessage(convId, {
        role: "assistant",
        content: "",
        model: modelForMessage,
      });
      store.setStreaming(convId, assistantMsgId, true);

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

      const allMessages = [
        ...(currentConv?.messages ?? []).map((m) => ({
          role: m.role,
          content: m.content,
        })),
        { role: "user" as const, content },
      ];

      const callbacks = {
        onToken: (token: string) =>
          store.appendToMessage(convId!, assistantMsgId, token),
        onToolCallStart: (
          toolCall: Parameters<typeof store.addToolCall>[2],
        ) => {
          const tcId = store.addToolCall(convId!, assistantMsgId, toolCall);
          store.openSandbox(convId!, assistantMsgId);
          if (toolCall.type === "code_exec") {
            // Auto-expand right panel and switch to terminal tab
            setRightPanelOpen(true);
            setRightPanelTab("terminal");

            addTerminalLine(`$ Running ${toolCall.language ?? "code"}...`);
            if (toolCall.code) {
              toolCall.code
                .split("\n")
                .forEach((l) => addTerminalLine(`  ${l}`));
            }
          }
          return tcId;
        },
        onToolCallUpdate: (
          toolCallId: string,
          updates: Parameters<typeof store.updateToolCall>[3],
        ) => {
          store.updateToolCall(convId!, assistantMsgId, toolCallId, updates);
          if (updates.output) {
            updates.output.split("\n").forEach((l) => addTerminalLine(l));
          }
        },
        onDone: () => store.setStreaming(convId!, assistantMsgId, false),
        onError: (error: string) => {
          store.updateMessage(convId!, assistantMsgId, {
            content: `Sorry, I encountered an error: ${error}\n\nPlease make sure Ollama is running.`,
          });
          store.setStreaming(convId!, assistantMsgId, false);
        },
      };

      try {
        const res = await fetch("/api/chat/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            messages: allMessages,
            model: modelForMessage,
            stream: true,
          }),
        });
        if (res.ok) await streamChat(allMessages, modelForMessage, callbacks);
        else await streamChatDemo(content, callbacks);
      } catch {
        await streamChatDemo(content, callbacks);
      }
    },
    [
      activeConversationId,
      conv,
      store,
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
      {messages.length === 0 ? (
        <WelcomeScreen onSend={handleSend} />
      ) : (
        <ScrollArea className="flex-1">
          <div className="max-w-3xl mx-auto px-4 py-6 space-y-6">
            {messages.map((msg) => (
              <MessageBubble
                key={msg.id}
                message={msg}
                conversationId={conv!.id}
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
