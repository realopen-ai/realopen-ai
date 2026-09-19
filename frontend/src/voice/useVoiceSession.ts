/**
 * useVoiceSession — React hook that binds the VoiceSessionClient (WebSocket
 * + audio) to the voice UI store and to the EXISTING chat store, so voice
 * turns render exactly like text turns.
 *
 * Responsibilities:
 *   • Owns exactly ONE VoiceSessionClient per active voice session.
 *   • Maps server state events → voiceStore (voiceState, partial transcript,
 *     errors, mic permission).
 *   • On `user_message` (voice turn start): adds the persisted user message
 *     (modality "voice") + an empty streaming assistant message to the SAME
 *     chatStore conversation, exactly like ChatArea.handleSend does for
 *     text, then drives it with the SAME stream callbacks (built by
 *     ChatArea's buildStreamCallbacks — shared with the text path).
 *   • On `agent_event`: dispatches through the shared dispatchAgentEvent
 *     router (the same code the SSE parser uses), so thinking blocks, text
 *     tokens, tool_call blocks, rag sources and deliverables render
 *     identically for voice and text.
 *   • On `assistant_message`: finalizes the streaming assistant message
 *     with the final content/blocks/deliverables — also the path used when
 *     the generation was interrupted mid-stream (the server sends partial
 *     content), guaranteeing no duplicate assistant messages and no stale
 *     streaming state.
 *   • On `interrupted`: triggers the assistant-bubble flash (voiceStore)
 *     — audio is already stopped instantly inside the client.
 *
 * Text chat is NOT routed through this hook — text keeps using the existing
 * /api/chat/stream path unchanged.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { dispatchAgentEvent, type StreamCallbacks } from "@/api/stream";
import { type MessageDTO } from "@/api/client";
import { dtoToMessage, useChatStore } from "@/store/chatStore";
import { useVoiceStore, type VoiceUiState } from "@/voice/voiceStore";
import {
  VoiceSessionClient,
  type VoiceAssistantMessageEvent,
  type VoiceUserMessageEvent,
} from "@/voice/VoiceSessionClient";
import { createDebugLogger } from "@/lib/debug";

const log = createDebugLogger("useVoiceSession");

// ─── Options ──────────────────────────────────────────────────────

export interface BuildStreamCallbacksOptions {
  /** The user message the assistant is responding to (for digest progress). */
  userMsgId?: string;
  /** Content of that user message. */
  content: string;
  /** True when this turn created the conversation from the home page —
   * the shared onDone callback navigates to /{convId} after the stream. */
  isFromHomePage: boolean;
}

export interface UseVoiceSessionOptions {
  /** The currently open conversation (null on the home page). */
  conversationId: string | null;
  /**
   * Builds the SAME stream callbacks object the text path uses — extracted
   * from ChatArea.handleSend into a reusable function so voice responses
   * render identically (thinking blocks, text tokens, tool_call blocks,
   * rag sources, deliverables, generation duration, navigation).
   */
  buildStreamCallbacks: (
    convId: string,
    assistantMsgId: string,
    opts: BuildStreamCallbacksOptions,
  ) => StreamCallbacks;
  /** Called when the hook had to create a conversation itself (voice
   * toggled from the home page) — mirrors handleSend's setPendingConvId. */
  onConversationCreated?: (convId: string) => void;
}

export interface VoiceSessionHandle {
  /** Toggle: connect+start / stop. Clicking while SPEAKING = interrupt. */
  toggleVoice: () => void;
  /** Explicit barge-in affordance (mic button / status strip). */
  interruptSpeaking: () => void;
  /** True while a voice session is connecting or active. */
  isVoiceActive: boolean;
  /** Current voice UI state (mirrored from the session). */
  voiceState: VoiceUiState;
}

// ─── Turn bookkeeping ─────────────────────────────────────────────

interface VoiceTurn {
  convId: string;
  assistantMsgId: string;
  userMsgId: string;
  callbacks: StreamCallbacks;
}

// ─── Hook ─────────────────────────────────────────────────────────

export function useVoiceSession(
  options: UseVoiceSessionOptions,
): VoiceSessionHandle {
  // Keep the latest options accessible from event handlers without
  // re-wiring the client.
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const clientRef = useRef<VoiceSessionClient | null>(null);
  const turnRef = useRef<VoiceTurn | null>(null);
  // Set to true when the voice session itself created the conversation
  // (toggled from the home page) — used as isFromHomePage for callbacks.
  const voiceCreatedHomeConversationRef = useRef(false);
  const [isVoiceActive, setIsVoiceActive] = useState(false);
  const voiceState = useVoiceStore((s) => s.voiceState);

  // Fetch backend voice readiness once per mount (cheap GET — also warms
  // the mic-button tooltip when setup is incomplete).
  useEffect(() => {
    void useVoiceStore.getState().fetchVoiceStatus();
  }, []);

  // ── Voice turn lifecycle ────────────────────────────────────────

  /**
   * Voice turn start (`user_message` event): mirror ChatArea.handleSend —
   * add the persisted user message (modality "voice") + an empty streaming
   * assistant message to the SAME store, then build the shared callbacks.
   */
  const startVoiceTurn = useCallback((userMsg: VoiceUserMessageEvent) => {
    const client = clientRef.current;
    if (!client) return;
    const convId = client.activeConversationId;
    if (!convId) return;
    const store = useChatStore.getState();
    const voiceStore = useVoiceStore.getState();

    log(
      `voice turn start  convId=${convId}  content="${userMsg.content.slice(0, 60)}"`,
    );

    // Replace the transient partial-transcript bubble with the persisted
    // user message.
    voiceStore.setPartialTranscript("");

    // Finalize any still-streaming turn (e.g. the previous generation was
    // interrupted and the server is still winding it down) so there is
    // never a stale streaming state.
    const prevTurn = turnRef.current;
    if (prevTurn) {
      const prevMsg = useChatStore
        .getState()
        .conversations.find((c) => c.id === prevTurn.convId)
        ?.messages.find((m) => m.id === prevTurn.assistantMsgId);
      if (prevMsg?.isStreaming) {
        store.setStreaming(prevTurn.convId, prevTurn.assistantMsgId, false);
      }
      turnRef.current = null;
    }

    // ── Same structure as handleSend: user message + empty assistant ──
    const userMsgId = store.addMessage(convId, {
      role: "user",
      content: userMsg.content,
      modality: userMsg.modality === "voice" ? "voice" : "text",
    });
    const modelForMessage = store.selectedModel;
    const assistantMsgId = store.addMessage(convId, {
      role: "assistant",
      content: "",
      model: modelForMessage,
    });
    store.setStreaming(convId, assistantMsgId, true);

    // Optimistic preview title from the first user message — identical to
    // handleSend (the backend's conversation_title event replaces it).
    const currentConv = useChatStore
      .getState()
      .conversations.find((c) => c.id === convId);
    if (
      currentConv &&
      currentConv.messages.length <= 2 &&
      (!currentConv.title || currentConv.title === "New Chat")
    ) {
      const title =
        userMsg.content.length > 40
          ? userMsg.content.slice(0, 40) + "..."
          : userMsg.content;
      useChatStore.setState((s) => ({
        conversations: s.conversations.map((c) =>
          c.id === convId ? { ...c, title } : c,
        ),
      }));
    }

    // The conversation was created by the voice toggle from the home page
    // (see toggleVoice) — the shared onDone navigates to /{convId}.
    const isFromHomePage = voiceCreatedHomeConversationRef.current;

    turnRef.current = {
      convId,
      assistantMsgId,
      userMsgId,
      callbacks: optionsRef.current.buildStreamCallbacks(
        convId,
        assistantMsgId,
        {
          userMsgId,
          content: userMsg.content,
          isFromHomePage,
        },
      ),
    };
  }, []);

  // Set to true when the voice session itself created the conversation
  // (toggled from the home page) — used as isFromHomePage for callbacks.
  // (Declared above with the other refs.)

  /**
   * `assistant_message` event: finalize the streaming assistant message
   * with the final persisted content/blocks/deliverables. This is also the
   * interruption path — the server sends partial content — so there are no
   * duplicate assistant messages and no stale streaming state.
   */
  const finalizeVoiceTurn = useCallback((msg: VoiceAssistantMessageEvent) => {
    const turn = turnRef.current;
    if (!turn) return;
    const store = useChatStore.getState();

    // Only finalize while the turn's assistant message is still streaming
    // (protects against a late duplicate after a newer turn began).
    const streaming = useChatStore
      .getState()
      .conversations.find((c) => c.id === turn.convId)
      ?.messages.find((m) => m.id === turn.assistantMsgId)?.isStreaming;
    if (!streaming) {
      turnRef.current = null;
      return;
    }

    log(
      `voice turn finalize  content="${msg.content.slice(0, 60)}"  blocks=${msg.blocks?.length ?? 0}`,
    );

    // Convert the persisted message (same DTO shape) and merge the final
    // fields into the live message.
    const finalMsg = dtoToMessage({
      id: msg.id,
      conversationId: turn.convId,
      role: "assistant",
      content: msg.content,
      model: msg.model ?? null,
      tokens: null,
      hasImage: false,
      hasDocument: false,
      imageCount: 0,
      documentCount: 0,
      createdAt: Date.now(),
      modality: msg.modality === "voice" ? "voice" : undefined,
      blocks: msg.blocks as MessageDTO["blocks"],
      deliverables: msg.deliverables as MessageDTO["deliverables"],
    } satisfies MessageDTO);

    store.updateMessage(turn.convId, turn.assistantMsgId, {
      content: finalMsg.content,
      blocks: finalMsg.blocks,
      deliverables: finalMsg.deliverables,
      modality: finalMsg.modality,
    });
    store.setStreaming(turn.convId, turn.assistantMsgId, false);
    turnRef.current = null;
  }, []);

  // ── Session wiring ──────────────────────────────────────────────

  const teardownSession = useCallback(() => {
    // Finalize any in-flight voice turn so the UI never hangs in a
    // streaming state.
    const turn = turnRef.current;
    if (turn) {
      const streaming = useChatStore
        .getState()
        .conversations.find((c) => c.id === turn.convId)
        ?.messages.find((m) => m.id === turn.assistantMsgId)?.isStreaming;
      if (streaming) {
        useChatStore
          .getState()
          .setStreaming(turn.convId, turn.assistantMsgId, false);
      }
      turnRef.current = null;
    }
    clientRef.current?.destroy();
    clientRef.current = null;
    voiceCreatedHomeConversationRef.current = false;
    useVoiceStore.getState().reset();
    setIsVoiceActive(false);
  }, []);

  const wireClient = useCallback(
    (client: VoiceSessionClient) => {
      client.on("state", (s) => {
        const map: Record<string, VoiceUiState> = {
          LISTENING: "listening",
          PROCESSING: "processing",
          SPEAKING: "speaking",
          INTERRUPTING: "interrupting",
          STOPPING: "stopping",
          ERROR: "error",
        };
        const ui = map[s.state] ?? "listening";
        useVoiceStore.getState().setVoiceState(ui);
      });

      client.on("ready", () => {
        useVoiceStore.getState().setMicPermission("granted");
        useVoiceStore.getState().setVoiceState("listening");
      });

      client.on("asrPartial", (p) => {
        useVoiceStore.getState().setPartialTranscript(p.text);
      });
      client.on("asrFinal", (p) => {
        // Keep the final text visible as the partial bubble until the
        // persisted user_message replaces it.
        useVoiceStore.getState().setPartialTranscript(p.text);
      });

      client.on("userMessage", (m) => startVoiceTurn(m));

      client.on("agentEvent", (e) => {
        const turn = turnRef.current;
        if (!turn) return;
        // Same event shapes as the SSE stream → same dispatcher → identical
        // rendering (thinking, tokens, tool calls, sources, deliverables).
        dispatchAgentEvent(e, turn.callbacks);
      });

      client.on("assistantMessage", (m) => finalizeVoiceTurn(m));

      client.on("interrupted", () => {
        // Audio already stopped instantly inside the client — flash the
        // assistant bubble as interruption feedback.
        useVoiceStore.getState().setInterruptFlash();
      });

      client.on("error", (e) => {
        useVoiceStore.getState().setVoiceError({
          code: e.code,
          message: e.message,
        });
        if (e.code === "mic_denied") {
          useVoiceStore.getState().setMicPermission("denied");
        } else if (e.code === "mic_unavailable") {
          useVoiceStore.getState().setMicPermission("unavailable");
        }
        if (e.fatal) {
          useVoiceStore.getState().setVoiceState("error");
          teardownSession();
        }
      });

      client.on("stopped", () => {
        teardownSession();
      });
    },
    [startVoiceTurn, finalizeVoiceTurn, teardownSession],
  );

  // ── Toggle ──────────────────────────────────────────────────────

  const startVoiceSession = useCallback(async () => {
    const voiceStore = useVoiceStore.getState();
    voiceStore.clearVoiceError();

    // Gate on backend readiness (fetched on mount / refreshed here).
    let readiness = voiceStore.readiness;
    if (!readiness) {
      readiness = await voiceStore.fetchVoiceStatus();
    }
    if (readiness && !readiness.ready) {
      voiceStore.setVoiceError({
        code: "not_ready",
        message: "Voice is not set up yet — complete the setup wizard first.",
      });
      return;
    }

    // The voice WebSocket binds to a conversation_id — create one when
    // voice is toggled from the home page (mirrors handleSend's behavior
    // of creating the conversation on first send).
    let convId = optionsRef.current.conversationId;
    voiceCreatedHomeConversationRef.current = false;
    if (!convId) {
      log("no active conversation — creating one for voice");
      convId = await useChatStore.getState().createConversation();
      if (!convId) return;
      voiceCreatedHomeConversationRef.current = true;
      optionsRef.current.onConversationCreated?.(convId);
    }

    const client = new VoiceSessionClient();
    wireClient(client);
    clientRef.current = client;
    setIsVoiceActive(true);
    useVoiceStore.getState().setVoiceState("connecting");

    try {
      await client.connect(convId);
      log("voice session ready");
    } catch {
      // Error event already emitted by the client → shown in the status
      // strip. Clean up the dead client.
      teardownSession();
    }
  }, [wireClient, teardownSession]);

  const toggleVoice = useCallback(() => {
    const client = clientRef.current;
    if (client) {
      const state = useVoiceStore.getState().voiceState;
      if (state === "speaking") {
        // Clicking the mic while the assistant speaks = barge-in.
        client.interrupt();
      } else {
        client.stop();
      }
      return;
    }
    void startVoiceSession();
  }, [startVoiceSession]);

  const interruptSpeaking = useCallback(() => {
    clientRef.current?.interrupt();
  }, []);

  // ── Conversation switch / unmount cleanup ───────────────────────

  // The WebSocket is bound to one conversation. If the user opens a
  // different conversation while voice is active, stop the session (the
  // user can re-toggle — reconnection is deliberately NOT automatic).
  useEffect(() => {
    const client = clientRef.current;
    if (
      client &&
      options.conversationId &&
      client.activeConversationId &&
      client.activeConversationId !== options.conversationId
    ) {
      log("conversation changed — stopping voice session");
      client.stop();
    }
  }, [options.conversationId]);

  // Unmount: destroy the session entirely.
  useEffect(() => {
    return () => {
      clientRef.current?.destroy();
      clientRef.current = null;
    };
  }, []);

  return { toggleVoice, interruptSpeaking, isVoiceActive, voiceState };
}
