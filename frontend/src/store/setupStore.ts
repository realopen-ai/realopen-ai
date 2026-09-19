import { create } from "zustand";
import {
  fetchSetupStatus,
  fetchHardwareInfo,
  fetchSetupProfiles,
  fetchSetupModules,
  applySetup as apiApplySetup,
  pullSetupModels as apiPullModels,
  completeSetup as apiCompleteSetup,
  type HardwareInfo,
  type SetupProfile,
  type SetupModule,
  type PullModelEvent,
} from "@/api/setupClient";

// ─── Types ───────────────────────────────────────────────────────

export type SetupStep =
  | "welcome"
  | "prerequisites"
  | "hardware"
  | "modules"
  | "review"
  | "installing"
  | "done";

/** Provider of a setup artifact (models + voice models + runtimes). */
export type PullProvider = "ollama" | "qwen3-asr" | "pocket-tts" | "pip";

/** Kind of a setup artifact — ollama LLM models, voice models (ASR/TTS)
 *  or voice runtime packages (ffmpeg & co). */
export type PullKind = "ollama" | "voice_model" | "voice_runtime";

/** Per-row install state — unified list covering ollama models AND voice
 *  models/runtimes (ASR, TTS, runtime packages). */
export interface PullRow {
  model: string;
  module?: string;
  provider?: PullProvider;
  kind?: PullKind;
  status: "pending" | "running" | "done" | "error";
  /** Real percentage — only set when the backend sends pull_progress
   *  events with actual percent values (NO fake percentages). */
  percent?: number;
  /** Whether real percentages have been received for this row. */
  hasPercent: boolean;
  /** Latest streamed status text (pull_status events). */
  statusText?: string;
  /** Latest streamed output text (pull_status events). */
  outputText?: string;
  error?: string;
  /** pull_done with already_installed — the artifact was present. */
  alreadyInstalled?: boolean;
}

export interface PullProgress {
  currentModel: string;
  currentModule: string;
  currentIndex: number;
  totalModels: number;
  percent: number;
  status: string;
  errors: string[];
  completed: string[];
  rows: PullRow[];
}

// ─── Store ───────────────────────────────────────────────────────

interface SetupState {
  // Setup status
  setupComplete: boolean;
  isChecking: boolean;

  // Current step
  step: SetupStep;

  // Hardware info
  hardwareInfo: HardwareInfo | null;
  isLoadingHardware: boolean;

  // Profiles
  profiles: Record<string, SetupProfile>;
  isLoadingProfiles: boolean;
  selectedProfile: string;

  // Modules
  modules: SetupModule[];
  isLoadingModules: boolean;
  enabledModules: string[];

  // Model pulling
  isApplying: boolean;
  isPulling: boolean;
  pullProgress: PullProgress;

  // Error state
  error: string | null;

  // Actions
  checkSetupStatus: () => Promise<boolean>;
  loadHardwareInfo: () => Promise<void>;
  loadProfiles: () => Promise<void>;
  loadModules: () => Promise<void>;
  setSelectedProfile: (profile: string) => void;
  toggleModule: (name: string) => void;
  setStep: (step: SetupStep) => void;
  applySetup: () => Promise<boolean>;
  pullModels: () => Promise<boolean>;
  completeSetup: () => Promise<void>;
  setError: (error: string | null) => void;
  reset: () => void;
}

const initialPullProgress: PullProgress = {
  currentModel: "",
  currentModule: "",
  currentIndex: 0,
  totalModels: 0,
  percent: 0,
  status: "",
  errors: [],
  completed: [],
  rows: [],
};

/** Find-or-create a row by model name inside a pullProgress update. */
function upsertRow(
  rows: PullRow[],
  model: string,
  patch: Partial<PullRow>,
): PullRow[] {
  const idx = rows.findIndex((r) => r.model === model);
  if (idx === -1) {
    return [...rows, { model, status: "running", hasPercent: false, ...patch }];
  }
  const next = [...rows];
  next[idx] = { ...next[idx], ...patch };
  return next;
}

export const useSetupStore = create<SetupState>((set, get) => ({
  setupComplete: false,
  isChecking: true,
  step: "welcome",
  hardwareInfo: null,
  isLoadingHardware: false,
  profiles: {},
  isLoadingProfiles: false,
  selectedProfile: "cpu_small",
  modules: [],
  isLoadingModules: false,
  enabledModules: ["assistant"],
  isApplying: false,
  isPulling: false,
  pullProgress: { ...initialPullProgress },
  error: null,

  checkSetupStatus: async () => {
    set({ isChecking: true });
    try {
      const status = await fetchSetupStatus();
      set({ setupComplete: status.setup_complete, isChecking: false });
      return status.setup_complete;
    } catch {
      set({ isChecking: false });
      return false;
    }
  },

  loadHardwareInfo: async () => {
    set({ isLoadingHardware: true });
    try {
      const info = await fetchHardwareInfo();
      if (info) {
        set({
          hardwareInfo: info,
          selectedProfile: info.recommended_profile,
          isLoadingHardware: false,
        });
      } else {
        set({ isLoadingHardware: false });
      }
    } catch {
      set({ isLoadingHardware: false });
    }
  },

  loadProfiles: async () => {
    set({ isLoadingProfiles: true });
    try {
      const profiles = await fetchSetupProfiles();
      if (profiles) {
        set({ profiles, isLoadingProfiles: false });
      } else {
        set({ isLoadingProfiles: false });
      }
    } catch {
      set({ isLoadingProfiles: false });
    }
  },

  loadModules: async () => {
    set({ isLoadingModules: true });
    try {
      const modules = await fetchSetupModules();
      if (modules) {
        // Auto-enable required modules
        const enabled = modules.filter((m) => m.required).map((m) => m.name);
        set({ modules, enabledModules: enabled, isLoadingModules: false });
      } else {
        set({ isLoadingModules: false });
      }
    } catch {
      set({ isLoadingModules: false });
    }
  },

  setSelectedProfile: (profile) => set({ selectedProfile: profile }),

  toggleModule: (name) => {
    const state = get();
    const module = state.modules.find((m) => m.name === name);
    if (!module || module.required) return;

    // Check if module is available for the selected profile
    if (!module.availability[state.selectedProfile]) return;

    set({
      enabledModules: state.enabledModules.includes(name)
        ? state.enabledModules.filter((n) => n !== name)
        : [...state.enabledModules, name],
    });
  },

  setStep: (step) => set({ step, error: null }),

  applySetup: async () => {
    const state = get();
    set({ isApplying: true, error: null });
    try {
      const result = await apiApplySetup(
        state.selectedProfile,
        state.enabledModules,
      );
      if (result) {
        set({ isApplying: false });
        return true;
      }
      set({
        isApplying: false,
        error: "Failed to apply setup configuration",
      });
      return false;
    } catch (err) {
      set({ isApplying: false, error: String(err) });
      return false;
    }
  },

  pullModels: async () => {
    const state = get();
    set({
      isPulling: true,
      pullProgress: { ...initialPullProgress },
      error: null,
    });

    const completed: string[] = [];
    const errors: string[] = [];

    const success = await apiPullModels(
      state.selectedProfile,
      state.enabledModules,
      (event: PullModelEvent) => {
        const evt = event.event;

        if (evt === "pull_start_all") {
          set((s) => ({
            pullProgress: {
              ...s.pullProgress,
              totalModels: event.total ?? 0,
            },
          }));
        } else if (evt === "pull_start") {
          set((s) => ({
            pullProgress: {
              ...s.pullProgress,
              currentModel: event.model ?? "",
              currentModule: event.module ?? "",
              currentIndex: event.index ?? 0,
              status: "pulling",
              rows: upsertRow(s.pullProgress.rows, event.model ?? "", {
                module: event.module,
                provider: event.provider,
                kind: event.kind,
                status: "running",
              }),
            },
          }));
        } else if (evt === "pull_progress") {
          set((s) => ({
            pullProgress: {
              ...s.pullProgress,
              percent: event.percent ?? 0,
              status: event.status ?? "pulling",
              rows:
                event.model != null
                  ? upsertRow(s.pullProgress.rows, event.model, {
                      percent: event.percent ?? 0,
                      hasPercent: true,
                    })
                  : s.pullProgress.rows,
            },
          }));
        } else if (evt === "pull_status") {
          // Streamed status/output lines WITHOUT percentages — typical for
          // voice model/runtime installs (pip, HF downloads). The row keeps
          // an indeterminate animated bar (no fake percentages).
          set((s) => ({
            pullProgress: {
              ...s.pullProgress,
              rows:
                event.model != null
                  ? upsertRow(s.pullProgress.rows, event.model, {
                      provider: event.provider,
                      kind: event.kind,
                      statusText: event.status,
                      outputText: event.output,
                    })
                  : s.pullProgress.rows,
            },
          }));
        } else if (evt === "pull_done") {
          completed.push(event.model ?? "");
          set((s) => ({
            pullProgress: {
              ...s.pullProgress,
              completed: [...completed],
              percent: 100,
              rows: upsertRow(s.pullProgress.rows, event.model ?? "", {
                status: "done",
                percent: 100,
                hasPercent: true,
                alreadyInstalled: event.already_installed ?? false,
              }),
            },
          }));
        } else if (evt === "pull_error") {
          errors.push(`${event.model}: ${event.error}`);
          set((s) => ({
            pullProgress: {
              ...s.pullProgress,
              errors: [...errors],
              rows: upsertRow(s.pullProgress.rows, event.model ?? "", {
                status: "error",
                error: event.error,
              }),
            },
          }));
        } else if (evt === "pull_all_done") {
          set((s) => ({
            pullProgress: {
              ...s.pullProgress,
              percent: 100,
              status: "done",
            },
          }));
        } else if (evt === "setup_error") {
          set((s) => ({
            pullProgress: {
              ...s.pullProgress,
              errors: [
                ...s.pullProgress.errors,
                event.error ?? "Unknown error",
              ],
            },
          }));
        }
      },
    );

    set({ isPulling: false });
    return success;
  },

  completeSetup: async () => {
    const success = await apiCompleteSetup();
    if (success) {
      set({ setupComplete: true, step: "done" });
    }
  },

  setError: (error) => set({ error }),

  reset: () =>
    set({
      step: "welcome",
      error: null,
      pullProgress: { ...initialPullProgress },
    }),
}));
