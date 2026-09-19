/**
 * VoiceSessionClient — browser-side implementation of the RealOpen-AI
 * Voice WebSocket Protocol v1.
 *
 * Owns:
 *   • The WebSocket to `ws(s)://${location.host}/ws/voice?conversation_id=…`
 *   • The AudioContext + getUserMedia mic capture (with REAL browser AEC:
 *     echoCancellation / noiseSuppression / autoGainControl — stage 1 of
 *     the AEC pipeline).
 *   • An AudioWorklet capture node ('voice-capture-processor') whose PCM
 *     output is framed as `0x01` binary frames (s16le 16 kHz mono,
 *     ~100 ms). Mic frames are ALWAYS sent — including while assistant
 *     audio plays (barge-in requirement; the mic never stops).
 *   • An AudioWorklet playback node ('voice-playback-processor') fed by
 *     `0x03` binary frames (s16le 24 kHz TTS chunks). Every `played`
 *     buffer the worklet reports (the EXACT frames scheduled to the
 *     speaker) is downsampled to 16 kHz and echoed back to the server as
 *     a `0x02` far-end reference frame, in lockstep.
 *   • Client-side barge-in: while the session is in the SPEAKING state, a
 *     light energy VAD (RMS + adaptive threshold, N consecutive frames)
 *     runs on the captured PCM; speech onset triggers `interrupt()` —
 *     belt-and-braces with the server-side VAD.
 *   • TTS generation gating: `tts_chunk` announces (generation_id + seq +
 *     sample_rate) must immediately precede their `0x03` binary frame;
 *     binary audio of stale/interrupted generations or without a matching
 *     announce is dropped.
 *   • ping/pong keepalive (client ping every 25 s).
 *   • Robust teardown: WS close → error event + cleanup; NO automatic
 *     reconnect (the user re-toggles the mic button).
 *
 * Text chat is NOT handled here — text goes through the existing
 * /api/chat/stream path. This client is voice-only.
 */

import { createDebugLogger, dbgError } from "@/lib/debug";

const log = createDebugLogger("voice");

// ─── Protocol constants ───────────────────────────────────────────

/** Binary frame prefixes (client → server and server → client). */
const FRAME_MIC = 0x01; // client → server, mic PCM s16le 16k mono
const FRAME_FAR_END = 0x02; // client → server, far-end reference s16le 16k mono
const FRAME_TTS = 0x03; // server → client, TTS PCM s16le 24k mono

/** Sample rates. */
const MIC_RATE = 16000;
const TTS_RATE = 24000;

/** Keepalive: client ping interval (protocol: every 25 s). */
const PING_INTERVAL_MS = 25_000;

/** Grace period after `stop` before force-closing the socket. */
const STOP_TIMEOUT_MS = 3_000;

/** How long to wait for the server's `ready` event after the socket
 * opens before treating the session as failed. */
const READY_TIMEOUT_MS = 15_000;

// ─── Client-side barge-in VAD tuning ───────────────────────────────
/** Speech onset = N consecutive ~100 ms frames above the threshold. */
const VAD_CONSECUTIVE_FRAMES = 3;
/** Absolute RMS floor — echo residue after browser AEC stays well below. */
const VAD_MIN_RMS = 0.02;
/** Adaptive threshold = noise floor × multiplier. */
const VAD_NOISE_MULTIPLIER = 5;
/** Exponential moving average factor for the noise floor. */
const VAD_NOISE_EMA = 0.05;

// ─── Event payload types ───────────────────────────────────────────

export type VoiceSessionState =
  | "LISTENING"
  | "PROCESSING"
  | "SPEAKING"
  | "INTERRUPTING"
  | "STOPPING"
  | "ERROR";

export interface VoiceReadyEvent {
  session_id: string;
  state?: string;
  asr?: unknown;
  tts?: unknown;
}

export interface VoiceStateEvent {
  state: VoiceSessionState | string;
  reason?: string;
  generation_id?: string;
}

export interface VoiceAsrEvent {
  utterance_id: string;
  text: string;
}

export interface VoiceUserMessageEvent {
  id: string;
  role: string;
  content: string;
  modality?: string;
}

/** agent_event: `{ event: {...} }` where the inner object has the SAME
 * shape as the existing SSE stream events (message / thinking_start /
 * thinking / thinking_done / tool_call / rag_sources / deliverables /
 * generation_done / error / done). */
export interface VoiceAgentEvent {
  event: string;
  [key: string]: unknown;
}

export interface VoiceAssistantMessageEvent {
  id: string;
  role: string;
  content: string;
  blocks?: unknown[] | null;
  deliverables?: unknown[] | null;
  modality?: string;
  model?: string | null;
  generationDuration?: number;
}

export interface VoiceTtsAnnounce {
  phase: "start" | "chunk" | "end";
  generation_id: string;
  seq?: number;
  sample_rate?: number;
}

export interface VoiceErrorEvent {
  code: string;
  message: string;
  fatal?: boolean;
}

export interface VoiceEventMap {
  state: VoiceStateEvent;
  asrPartial: VoiceAsrEvent;
  asrFinal: VoiceAsrEvent;
  userMessage: VoiceUserMessageEvent;
  agentEvent: VoiceAgentEvent;
  assistantMessage: VoiceAssistantMessageEvent;
  ttsAnnounce: VoiceTtsAnnounce;
  interrupted: { generation_id: string };
  error: VoiceErrorEvent;
  ready: VoiceReadyEvent;
  stopped: void;
}

export type VoiceEventName = keyof VoiceEventMap;

// ─── Client ───────────────────────────────────────────────────────

export class VoiceSessionClient {
  // WebSocket
  private ws: WebSocket | null = null;
  private conversationId = "";
  private started = false;
  private stopping = false;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private stopTimer: ReturnType<typeof setTimeout> | null = null;
  private destroyed = false;

  // Audio graph
  private audioCtx: AudioContext | null = null;
  private micStream: MediaStream | null = null;
  private captureNode: AudioWorkletNode | null = null;
  private playbackNode: AudioWorkletNode | null = null;

  // TTS generation gating: a tts_chunk announce must immediately precede
  // its 0x03 binary frame. `expectTtsFrame` is armed by the announce and
  // consumed by the next binary frame; audio from stale/interrupted
  // generations is dropped.
  private activeTtsGeneration: string | null = null;
  private interruptedGenerations = new Set<string>();
  private expectTtsFrame = false;
  private ttsSampleRate = TTS_RATE;

  // Session state (from server `state` events) — used by the barge-in VAD.
  private sessionState: VoiceSessionState | "IDLE" = "IDLE";

  // Client-side barge-in VAD state
  private noiseFloor = 0.01;
  private vadAboveCount = 0;

  // Event emitter
  private listeners = new Map<VoiceEventName, Set<(payload: never) => void>>();

  // ─── Event emitter API ──────────────────────────────────────────

  on<K extends VoiceEventName>(
    name: K,
    handler: (payload: VoiceEventMap[K]) => void,
  ): () => void {
    let set = this.listeners.get(name);
    if (!set) {
      set = new Set();
      this.listeners.set(name, set);
    }
    set.add(handler as (payload: never) => void);
    return () => {
      set?.delete(handler as (payload: never) => void);
    };
  }

  private emit<K extends VoiceEventName>(
    name: K,
    payload: VoiceEventMap[K],
  ): void {
    const set = this.listeners.get(name);
    if (!set) return;
    for (const handler of set) {
      try {
        (handler as (p: VoiceEventMap[K]) => void)(payload);
      } catch (err) {
        dbgError(`voice event handler error (${name}):`, err);
      }
    }
  }

  private clearListeners(): void {
    this.listeners.clear();
  }

  // ─── Public API ─────────────────────────────────────────────────

  get isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  get isStarted(): boolean {
    return this.started;
  }

  get activeConversationId(): string | null {
    return this.conversationId || null;
  }

  /**
   * Connect to the voice WebSocket for the given conversation and start
   * streaming. Sets up the audio graph (mic + worklets) first — this must
   * be called from a user gesture so the AudioContext can start and the
   * mic permission prompt has a click context.
   */
  async connect(conversationId: string): Promise<void> {
    if (this.destroyed) throw new Error("client destroyed");
    this.conversationId = conversationId;
    this.stopping = false;
    log(`connect  convId=${conversationId}`);

    let micErrorEmitted = false;
    try {
      // 1. AudioContext — must be created inside the user gesture.
      const ctx = new AudioContext();
      this.audioCtx = ctx;
      if (ctx.state === "suspended") {
        await ctx.resume();
      }

      // 2. Microphone with REAL browser AEC (stage 1 of the
      //    echo-cancellation pipeline). Permission errors surface as
      //    typed events.
      let stream: MediaStream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
          video: false,
        });
      } catch (err) {
        micErrorEmitted = true;
        const { code, message } = mapMicError(err);
        this.emit("error", { code, message });
        this.emit("state", { state: "ERROR", reason: message });
        throw err;
      }
      this.micStream = stream;

      // 3. AudioWorklet modules (served from /worklets/*).
      await ctx.audioWorklet.addModule("/worklets/voice-capture-processor.js");
      await ctx.audioWorklet.addModule("/worklets/voice-playback-processor.js");

      // 4. Capture path: mic → capture worklet → 0x01 frames to the
      //    server. NOTE: never gated on playback state — the mic keeps
      //    flowing while the assistant speaks (barge-in + server-side AEC
      //    need it).
      const source = ctx.createMediaStreamSource(stream);
      const capture = new AudioWorkletNode(ctx, "voice-capture-processor");
      capture.port.onmessage = (e: MessageEvent) => {
        const msg = e.data as { type?: string; buffer?: ArrayBuffer };
        if (msg?.type === "pcm" && msg.buffer) {
          this.handleMicPcm(msg.buffer);
        }
      };
      source.connect(capture);
      // The capture node produces no audible output, but routing it through
      // a zero-gain sink guarantees the render graph keeps pulling it (some
      // browsers suspend nodes with no path to the destination). Gain is 0
      // so the microphone is NEVER routed to the speakers (no echo).
      const sink = ctx.createGain();
      sink.gain.value = 0;
      capture.connect(sink);
      sink.connect(ctx.destination);
      this.captureNode = capture;

      // 5. Playback path: playback worklet → speakers; `played` buffers
      //    are echoed back as 0x02 far-end reference frames.
      const playback = new AudioWorkletNode(ctx, "voice-playback-processor");
      playback.port.onmessage = (e: MessageEvent) => {
        const msg = e.data as { type?: string; buffer?: ArrayBuffer };
        if (msg?.type === "played" && msg.buffer) {
          this.sendFarEndReference(msg.buffer);
        }
      };
      playback.connect(ctx.destination);
      this.playbackNode = playback;

      // 6. WebSocket.
      await this.openSocket();
    } catch (err) {
      // Setup failed somewhere (mic permission, worklet load, WS connect).
      await this.teardownAudio();
      if (!micErrorEmitted) {
        this.emit("error", {
          code: "connection_failed",
          message: err instanceof Error ? err.message : "Voice setup failed.",
        });
      }
      throw err;
    }

    // Resolve once the server confirms the session with `ready`. Fails on
    // error, on `stopped` (user toggled off during connect), or on a ready
    // timeout.
    return new Promise<void>((resolve, reject) => {
      const timeoutId = setTimeout(() => {
        cleanup();
        this.emit("error", {
          code: "ready_timeout",
          message: "Voice session did not initialize in time.",
        });
        this.handleClosed(true);
        reject(new Error("voice ready timeout"));
      }, READY_TIMEOUT_MS);
      const finish = (fn: () => void) => {
        clearTimeout(timeoutId);
        cleanup();
        fn();
      };
      const offReady = this.on("ready", () => finish(resolve));
      const offError = this.on("error", (e) =>
        finish(() => reject(new Error(e.message))),
      );
      const offStopped = this.on("stopped", () =>
        finish(() => reject(new Error("voice session stopped"))),
      );
      const cleanup = () => {
        offReady();
        offError();
        offStopped();
      };
    });
  }

  /** Send the session start frame (idempotent — normally sent
   * automatically right after the socket opens). */
  start(): void {
    if (!this.isConnected || this.started) return;
    this.started = true;
    this.sendJson({
      type: "start",
      conversation_id: this.conversationId,
      sample_rate: MIC_RATE,
      channels: 1,
      format: "pcm_s16le",
    });
    log("start frame sent");
  }

  /** User toggled voice off: send `stop`, wind down audio, close socket. */
  stop(): void {
    if (this.stopping) return;
    this.stopping = true;
    log("stop()");
    // Stop streaming mic audio immediately; the server still may send a
    // final `stopped` confirmation.
    this.started = false;
    this.emit("state", { state: "STOPPING" });
    this.sendJson({ type: "stop" });
    // Give the server a moment to reply with `stopped`.
    this.stopTimer = setTimeout(() => {
      log("stop timeout — force closing");
      this.handleClosed(true);
    }, STOP_TIMEOUT_MS);
  }

  /** Client-side barge-in: tell the server, and stop playback NOW. */
  interrupt(): void {
    if (!this.isConnected || this.stopping) return;
    log("interrupt (client barge-in)");
    this.clearPlayback();
    this.sendJson({ type: "interrupt" });
  }

  /** Immediately clear queued playback audio (barge-in / interrupted). */
  clearPlayback(): void {
    this.playbackNode?.port.postMessage({ type: "clear" });
    this.expectTtsFrame = false;
  }

  /** Full teardown: audio graph, timers, socket, listeners. */
  destroy(): void {
    this.handleClosed(true);
  }

  // ─── WebSocket plumbing ─────────────────────────────────────────

  private socketUrl(): string {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/ws/voice?conversation_id=${encodeURIComponent(this.conversationId)}`;
  }

  private openSocket(): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(this.socketUrl());
      ws.binaryType = "arraybuffer";
      this.ws = ws;

      ws.onopen = () => {
        log("WS open");
        this.start();
        this.startPing();
        resolve();
      };
      ws.onerror = () => {
        dbgError("voice WS error");
        // onclose always follows onerror; teardown happens there. Only a
        // pre-open failure rejects this promise directly.
        if (ws.readyState === WebSocket.CONNECTING) {
          reject(new Error("voice connection failed"));
        }
      };
      ws.onclose = () => {
        this.handleClosed(this.stopping);
      };
      ws.onmessage = (ev: MessageEvent) => {
        this.handleSocketMessage(ev);
      };
    });
  }

  private handleSocketMessage(ev: MessageEvent): void {
    if (typeof ev.data === "string") {
      this.handleTextFrame(ev.data);
    } else if (ev.data instanceof ArrayBuffer) {
      this.handleBinaryFrame(ev.data);
    }
  }

  // ─── Server → client text frames (JSON) ─────────────────────────

  private handleTextFrame(raw: string): void {
    let msg: Record<string, unknown>;
    try {
      msg = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      return; // skip malformed JSON
    }
    const type = msg.type as string | undefined;
    if (!type) return;
    log("⬅️ event", type);

    switch (type) {
      case "ready": {
        this.emit("ready", {
          session_id: String(msg.session_id ?? ""),
          state: msg.state as string | undefined,
          asr: msg.asr,
          tts: msg.tts,
        });
        break;
      }
      case "state": {
        const state = String(msg.state ?? "");
        this.sessionState = state as VoiceSessionState;
        this.emit("state", {
          state,
          reason: msg.reason as string | undefined,
          generation_id: msg.generation_id as string | undefined,
        });
        if (state === "ERROR" && typeof msg.reason === "string") {
          this.emit("error", {
            code: "state_error",
            message: msg.reason,
          });
        }
        if (state === "LISTENING") {
          // New turn / after barge-in — reset the client VAD onset window.
          this.vadAboveCount = 0;
        }
        break;
      }
      case "asr_partial": {
        this.emit("asrPartial", {
          utterance_id: String(msg.utterance_id ?? ""),
          text: String(msg.text ?? ""),
        });
        break;
      }
      case "asr_final": {
        this.emit("asrFinal", {
          utterance_id: String(msg.utterance_id ?? ""),
          text: String(msg.text ?? ""),
        });
        break;
      }
      case "user_message": {
        const m = (msg.message ?? {}) as Record<string, unknown>;
        this.emit("userMessage", {
          id: String(m.id ?? ""),
          role: String(m.role ?? "user"),
          content: String(m.content ?? ""),
          modality: m.modality as string | undefined,
        });
        break;
      }
      case "agent_event": {
        const event = (msg.event ?? {}) as Record<string, unknown>;
        this.emit("agentEvent", {
          event: String(event.event ?? ""),
          ...event,
        } as VoiceAgentEvent);
        break;
      }
      case "assistant_message": {
        const m = (msg.message ?? {}) as Record<string, unknown>;
        this.emit("assistantMessage", {
          id: String(m.id ?? ""),
          role: String(m.role ?? "assistant"),
          content: String(m.content ?? ""),
          blocks: (m.blocks as unknown[] | undefined) ?? null,
          deliverables: (m.deliverables as unknown[] | undefined) ?? null,
          modality: m.modality as string | undefined,
          model: (m.model as string | null) ?? null,
          generationDuration: m.generationDuration as number | undefined,
        });
        break;
      }
      case "tts_start": {
        const gid = String(msg.generation_id ?? "");
        this.activeTtsGeneration = gid;
        this.interruptedGenerations.delete(gid);
        this.ttsSampleRate = (msg.sample_rate as number) ?? TTS_RATE;
        // tts_start itself does not arm a binary frame — only tts_chunk
        // announces are immediately followed by 0x03 audio.
        this.emit("ttsAnnounce", {
          phase: "start",
          generation_id: gid,
          sample_rate: this.ttsSampleRate,
        });
        break;
      }
      case "tts_chunk": {
        const gid = String(msg.generation_id ?? "");
        if (this.interruptedGenerations.has(gid)) {
          // Stale generation — do not arm playback.
          this.expectTtsFrame = false;
          log(`dropping tts_chunk of interrupted generation ${gid}`);
          break;
        }
        this.activeTtsGeneration = gid;
        this.ttsSampleRate = (msg.sample_rate as number) ?? TTS_RATE;
        // Arm: the immediately following 0x03 binary frame belongs to this
        // announced chunk.
        this.expectTtsFrame = true;
        this.emit("ttsAnnounce", {
          phase: "chunk",
          generation_id: gid,
          seq: msg.seq as number | undefined,
          sample_rate: this.ttsSampleRate,
        });
        break;
      }
      case "tts_end": {
        const gid = String(msg.generation_id ?? "");
        this.expectTtsFrame = false;
        this.emit("ttsAnnounce", {
          phase: "end",
          generation_id: gid,
          sample_rate: (msg.sample_rate as number) ?? TTS_RATE,
        });
        break;
      }
      case "interrupted": {
        const gid = String(msg.generation_id ?? "");
        log(`⏹️ interrupted generation=${gid}`);
        // Stop playback NOW and drop any queued/in-flight 0x03 audio of
        // this generation.
        this.interruptedGenerations.add(gid);
        if (this.activeTtsGeneration === gid || !this.activeTtsGeneration) {
          this.clearPlayback();
        }
        this.emit("interrupted", { generation_id: gid });
        break;
      }
      case "error": {
        const code = String(msg.code ?? "voice_error");
        const message = String(msg.message ?? "Voice error");
        const fatal = Boolean(msg.fatal);
        this.emit("error", { code, message, fatal });
        if (fatal) {
          this.handleClosed(true);
        }
        break;
      }
      case "stopped": {
        log("stopped event");
        this.handleClosed(true);
        break;
      }
      case "pong":
        // Keepalive reply — nothing to do.
        break;
      default:
        // Unknown event type — ignore (forward compatibility).
        break;
    }
  }

  // ─── Server → client binary frames ──────────────────────────────

  private handleBinaryFrame(data: ArrayBuffer): void {
    if (data.byteLength < 1) return;
    const view = new Uint8Array(data);
    const prefix = view[0];
    if (prefix !== FRAME_TTS) {
      // Only 0x03 (TTS audio) is defined server → client.
      return;
    }
    // Drop binary audio that arrives without a matching tts_chunk announce
    // or that belongs to a stale/interrupted generation.
    if (!this.expectTtsFrame || !this.activeTtsGeneration) {
      log("dropping unannounced/stale 0x03 frame");
      return;
    }
    if (this.interruptedGenerations.has(this.activeTtsGeneration)) {
      this.expectTtsFrame = false;
      return;
    }
    this.expectTtsFrame = false; // one announce covers one binary frame
    const pcm = data.slice(1);
    if (pcm.byteLength === 0) return;
    this.playbackNode?.port.postMessage(
      { type: "append", buffer: pcm, sampleRate: this.ttsSampleRate },
      [pcm],
    );
  }

  // ─── Client → server frames ─────────────────────────────────────

  private sendJson(obj: Record<string, unknown>): void {
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    try {
      this.ws.send(JSON.stringify(obj));
    } catch (err) {
      dbgError("voice sendJson failed:", err);
    }
  }

  /** 0x01 mic frame — ALWAYS sent, including while assistant audio plays. */
  private handleMicPcm(buffer: ArrayBuffer): void {
    // Client-side barge-in VAD (runs on the near-end capture stream).
    this.runBargeInVad(buffer);
    if (this.ws?.readyState !== WebSocket.OPEN || !this.started) return;
    this.sendPrefixFrame(FRAME_MIC, buffer);
  }

  /** 0x02 far-end reference — the EXACT frames scheduled to the speaker
   *  (reported by the playback worklet), downsampled 24 k → 16 k. */
  private sendFarEndReference(played24k: ArrayBuffer): void {
    if (this.ws?.readyState !== WebSocket.OPEN || !this.started) return;
    if (played24k.byteLength < 2) return;
    const ref16k = resampleS16(played24k, TTS_RATE, MIC_RATE);
    if (ref16k.byteLength < 2) return;
    this.sendPrefixFrame(FRAME_FAR_END, ref16k);
  }

  private sendPrefixFrame(prefix: number, pcm: ArrayBuffer): void {
    const frame = new Uint8Array(1 + pcm.byteLength);
    frame[0] = prefix;
    frame.set(new Uint8Array(pcm), 1);
    try {
      this.ws?.send(frame.buffer);
    } catch (err) {
      dbgError("voice binary send failed:", err);
    }
  }

  // ─── Keepalive ──────────────────────────────────────────────────

  private startPing(): void {
    this.stopPing();
    this.pingTimer = setInterval(() => {
      this.sendJson({ type: "ping" });
    }, PING_INTERVAL_MS);
  }

  private stopPing(): void {
    if (this.pingTimer) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }

  // ─── Client-side barge-in VAD ───────────────────────────────────

  /**
   * Light energy VAD on the captured PCM. While the session is SPEAKING,
   * speech onset (N consecutive ~100 ms frames above an adaptive
   * threshold) triggers `interrupt()` — belt-and-braces with the
   * server-side VAD. The noise floor is tracked with an EMA so the
   * threshold adapts to the room.
   */
  private runBargeInVad(buffer: ArrayBuffer): void {
    if (buffer.byteLength < 2) return;
    const samples = new Int16Array(buffer);
    let sum = 0;
    for (let i = 0; i < samples.length; i++) {
      const v = samples[i] / 32768;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / samples.length);

    if (this.sessionState === "SPEAKING") {
      const threshold = Math.max(
        this.noiseFloor * VAD_NOISE_MULTIPLIER,
        VAD_MIN_RMS,
      );
      if (rms > threshold) {
        this.vadAboveCount++;
        if (this.vadAboveCount >= VAD_CONSECUTIVE_FRAMES) {
          log(
            `🎤 client barge-in VAD fired  rms=${rms.toFixed(3)}  threshold=${threshold.toFixed(3)}`,
          );
          this.vadAboveCount = 0;
          this.interrupt();
        }
      } else {
        this.vadAboveCount = 0;
      }
    } else {
      this.vadAboveCount = 0;
      // Track the noise floor from the non-speaking capture signal.
      this.noiseFloor =
        this.noiseFloor * (1 - VAD_NOISE_EMA) + rms * VAD_NOISE_EMA;
    }
  }

  // ─── Teardown ───────────────────────────────────────────────────

  /**
   * Single teardown entry point. `intentional` distinguishes a
   * user/server-initiated stop from an unexpected connection loss.
   */
  private handleClosed(intentional: boolean): void {
    if (this.destroyed) return;
    this.destroyed = true;

    this.stopPing();
    if (this.stopTimer) {
      clearTimeout(this.stopTimer);
      this.stopTimer = null;
    }
    const ws = this.ws;
    this.ws = null;
    if (
      ws &&
      (ws.readyState === WebSocket.OPEN ||
        ws.readyState === WebSocket.CONNECTING)
    ) {
      try {
        ws.onclose = null;
        ws.onerror = null;
        ws.onmessage = null;
        ws.close();
      } catch {
        /* ignore */
      }
    }
    void this.teardownAudio();
    this.started = false;
    this.stopping = false;
    this.activeTtsGeneration = null;
    this.interruptedGenerations.clear();
    this.expectTtsFrame = false;

    if (intentional) {
      this.emit("stopped", undefined);
    } else {
      // Unexpected close — surface as an error; reconnection is NOT
      // automatic (the user re-toggles the mic button).
      this.emit("error", {
        code: "connection_closed",
        message: "Voice connection closed unexpectedly.",
      });
      this.emit("state", { state: "ERROR", reason: "connection closed" });
    }
    this.clearListeners();
  }

  private async teardownAudio(): Promise<void> {
    // Disconnect the capture path first.
    if (this.captureNode) {
      try {
        this.captureNode.port.onmessage = null;
        this.captureNode.disconnect();
      } catch {
        /* ignore */
      }
      this.captureNode = null;
    }
    if (this.playbackNode) {
      try {
        this.playbackNode.port.onmessage = null;
        this.playbackNode.disconnect();
      } catch {
        /* ignore */
      }
      this.playbackNode = null;
    }
    if (this.micStream) {
      for (const track of this.micStream.getTracks()) {
        try {
          track.stop();
        } catch {
          /* ignore */
        }
      }
      this.micStream = null;
    }
    if (this.audioCtx) {
      const ctx = this.audioCtx;
      this.audioCtx = null;
      try {
        await ctx.close();
      } catch {
        /* ignore */
      }
    }
  }
}

// ─── Helpers ──────────────────────────────────────────────────────

/** Map a getUserMedia error to a typed voice error code + message. */
function mapMicError(err: unknown): { code: string; message: string } {
  const name =
    err instanceof DOMException
      ? err.name
      : err instanceof Error
        ? err.name
        : "";
  if (
    name === "NotAllowedError" ||
    name === "PermissionDeniedError" ||
    name === "SecurityError"
  ) {
    return {
      code: "mic_denied",
      message: "Microphone permission was denied.",
    };
  }
  if (
    name === "NotFoundError" ||
    name === "DevicesNotFoundError" ||
    name === "OverconstrainedError"
  ) {
    return {
      code: "mic_unavailable",
      message: "No microphone is available.",
    };
  }
  return {
    code: "mic_error",
    message: err instanceof Error ? err.message : "Microphone error.",
  };
}

/** Linear resampling of an s16le ArrayBuffer between two sample rates
 *  (used for the 24 kHz → 16 kHz far-end reference). */
function resampleS16(
  input: ArrayBuffer,
  fromRate: number,
  toRate: number,
): ArrayBuffer {
  if (fromRate === toRate) return input;
  const src = new Int16Array(input);
  const ratio = fromRate / toRate;
  const outLen = Math.max(1, Math.floor(src.length / ratio));
  const out = new Int16Array(outLen);
  let pos = 0;
  for (let i = 0; i < outLen; i++) {
    const i0 = Math.floor(pos);
    const i1 = Math.min(i0 + 1, src.length - 1);
    const frac = pos - i0;
    out[i] = Math.round(src[i0] * (1 - frac) + src[i1] * frac);
    pos += ratio;
  }
  return out.buffer;
}
