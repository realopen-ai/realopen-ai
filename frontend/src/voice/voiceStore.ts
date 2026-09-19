/**
 * voiceStore — zustand store for the voice-chat UI state.
 *
 * Follows the same style as the existing stores (chatStore, setupStore).
 * This store holds ONLY reactive UI state; the actual WebSocket/audio
 * session lives in VoiceSessionClient (see useVoiceSession.ts), which
 * pushes updates into this store.
 */

import { create } from "zustand";
import { createDebugLogger, dbgError } from "@/lib/debug";

const log = createDebugLogger("voiceStore");

// ─── Types ────────────────────────────────────────────────────────

export type VoiceUiState =
  | "inactive"
  | "connecting"
  | "listening"
  | "processing"
  | "speaking"
  | "interrupting"
  | "stopping"
  | "error";

export type MicPermission = "unknown" | "granted" | "denied" | "unavailable";

export interface VoiceReadiness {
  ready: boolean;
  enabled: boolean;
  asr: boolean;
  tts: boolean;
  missing: string[];
}

export interface VoiceError {
  code: string;
  message: string;
}

// ─── Store ────────────────────────────────────────────────────────

interface VoiceState {
  /** Voice session UI state — mirrors the server state machine
   * (LISTENING/PROCESSING/SPEAKING/INTERRUPTING/…) plus local states
   * (inactive/connecting/stopping). */
  voiceState: VoiceUiState;
  micPermission: MicPermission;
  /** Live partial ASR transcript (cleared when the persisted user
   * message replaces it). */
  partialTranscript: string;
  voiceError: VoiceError | null;
  /** Backend voice readiness (GET /api/voice/status). Null while
   * unknown. */
  readiness: VoiceReadiness | null;
  isFetchingStatus: boolean;
  /** Timestamp of the last assistant interruption — used by ChatArea to
   * briefly flash the assistant bubble border. */
  interruptFlashAt: number | null;

  // Actions
  setVoiceState: (state: VoiceUiState) => void;
  setMicPermission: (permission: MicPermission) => void;
  setPartialTranscript: (text: string) => void;
  setVoiceError: (error: VoiceError | null) => void;
  setReadiness: (readiness: VoiceReadiness | null) => void;
  setInterruptFlash: () => void;
  clearVoiceError: () => void;
  reset: () => void;
  fetchVoiceStatus: () => Promise<VoiceReadiness | null>;
}

/** Duration the assistant-bubble interruption flash stays visible (ms). */
export const INTERRUPT_FLASH_MS = 900;

export const useVoiceStore = create<VoiceState>((set) => ({
  voiceState: "inactive",
  micPermission: "unknown",
  partialTranscript: "",
  voiceError: null,
  readiness: null,
  isFetchingStatus: false,
  interruptFlashAt: null,

  setVoiceState: (state) => set({ voiceState: state }),
  setMicPermission: (permission) => set({ micPermission: permission }),
  setPartialTranscript: (text) => set({ partialTranscript: text }),
  setVoiceError: (error) => set({ voiceError: error }),
  setReadiness: (readiness) => set({ readiness }),
  setInterruptFlash: () => {
    set({ interruptFlashAt: Date.now() });
    setTimeout(() => {
      // Only clear if no newer flash started in the meantime.
      const current = useVoiceStore.getState().interruptFlashAt;
      if (current && Date.now() - current >= INTERRUPT_FLASH_MS - 50) {
        set({ interruptFlashAt: null });
      }
    }, INTERRUPT_FLASH_MS);
  },
  clearVoiceError: () => set({ voiceError: null }),
  reset: () =>
    set({
      voiceState: "inactive",
      partialTranscript: "",
      interruptFlashAt: null,
    }),

  /**
   * Voice readiness — with a two-step resolution:
   *   1. GET /api/voice/status (canonical voice endpoint). Response shape
   *      (defensively parsed): { ready, enabled, asr, tts, missing: string[] }
   *      where `asr`/`tts` may be booleans or nested status objects.
   *   2. FALLBACK: GET /api/setup/status → its `voice` summary
   *      (backend setup.py / voice_model_installer.voice_status_summary:
   *      { configured, ready, asr: {…, installed}, tts: {…, installed},
   *      runtime: [{id, display, installed}] }). Used when the voice
   *      endpoint is not available (e.g. voice router not yet registered).
   * Null only when BOTH endpoints are unreachable — the mic button then
   * stays enabled and any failure surfaces through the session error path.
   */
  fetchVoiceStatus: async () => {
    set({ isFetchingStatus: true });
    try {
      const res = await fetch("/api/voice/status");
      log(`fetchVoiceStatus  status=${res.status}`);
      if (res.ok) {
        const data = (await res.json()) as Record<string, unknown>;
        const flag = (v: unknown, fallback = false): boolean => {
          if (typeof v === "boolean") return v;
          if (v && typeof v === "object") {
            const obj = v as Record<string, unknown>;
            if (typeof obj.ready === "boolean") return obj.ready;
            if (typeof obj.installed === "boolean") return obj.installed;
          }
          return fallback;
        };
        const missingRaw = data.missing;
        const missing = Array.isArray(missingRaw)
          ? missingRaw.map((m) => String(m))
          : [];
        const readiness: VoiceReadiness = {
          ready: flag(data.ready, true),
          enabled: flag(data.enabled, true),
          asr: flag(data.asr, true),
          tts: flag(data.tts, true),
          missing,
        };
        set({ readiness, isFetchingStatus: false });
        return readiness;
      }
      // Voice endpoint missing / not registered — fall back to the setup
      // status voice summary (populated by the setup wizard backend).
      const readiness = await fetchVoiceReadinessFromSetup();
      set({ readiness, isFetchingStatus: false });
      return readiness;
    } catch (err) {
      dbgError("fetchVoiceStatus error:", err);
      set({ isFetchingStatus: false });
      return null;
    }
  },
}));

/** Map the /api/setup/status `voice` summary onto VoiceReadiness. */
async function fetchVoiceReadinessFromSetup(): Promise<VoiceReadiness | null> {
  try {
    const res = await fetch("/api/setup/status");
    if (!res.ok) {
      log(`voice readiness fallback (setup status)  status=${res.status}`);
      return null;
    }
    const data = (await res.json()) as Record<string, unknown>;
    const voice = data.voice as Record<string, unknown> | null | undefined;
    if (!voice || typeof voice !== "object") return null;

    const sideInstalled = (v: unknown): boolean | null => {
      if (typeof v === "boolean") return v;
      if (v && typeof v === "object") {
        const installed = (v as Record<string, unknown>).installed;
        if (typeof installed === "boolean") return installed;
      }
      return null;
    };
    const sideLabel = (v: unknown, fallback: string): string => {
      if (v && typeof v === "object") {
        const obj = v as Record<string, unknown>;
        const model = typeof obj.model === "string" ? obj.model : "";
        const desc = typeof obj.description === "string" ? obj.description : "";
        return model || desc || fallback;
      }
      return fallback;
    };

    const asrInstalled = sideInstalled(voice.asr);
    const ttsInstalled = sideInstalled(voice.tts);
    const runtime = Array.isArray(voice.runtime)
      ? (voice.runtime as Record<string, unknown>[])
      : [];

    // Human-readable missing artifacts for the mic-button tooltip.
    const missing: string[] = [];
    if (asrInstalled === false) {
      missing.push(`ASR: ${sideLabel(voice.asr, "model")}`);
    }
    if (ttsInstalled === false) {
      missing.push(`TTS: ${sideLabel(voice.tts, "model")}`);
    }
    for (const pkg of runtime) {
      if (pkg.installed === false) {
        missing.push(
          `Runtime: ${typeof pkg.display === "string" ? pkg.display : typeof pkg.id === "string" ? pkg.id : "package"}`,
        );
      }
    }

    const ready = voice.ready === true;
    const readiness: VoiceReadiness = {
      ready,
      enabled: voice.configured !== false,
      asr: asrInstalled ?? ready,
      tts: ttsInstalled ?? ready,
      missing,
    };
    log(
      `voice readiness (setup fallback)  ready=${ready}  configured=${String(voice.configured)}  missing=${missing.length}`,
    );
    return readiness;
  } catch (err) {
    dbgError("voice readiness fallback error:", err);
    return null;
  }
}
