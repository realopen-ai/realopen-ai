import { create } from "zustand";
import { modelManager, type ModelJob } from "@/api/modelManagerClient";
import { fetchModels } from "@/api/client";
import { useAiStore } from "@/store/aiStore";
import { useChatStore } from "@/store/chatStore";
import { activeModelJob } from "@/lib/modelManager";

interface ModelManagerState {
  jobs: ModelJob[];
  disconnected: boolean;
  revision: number;
  setJobs: (update: ModelJob[] | ((rows: ModelJob[]) => ModelJob[])) => void;
}
export const useModelManagerStore = create<ModelManagerState>((set) => ({
  jobs: [],
  disconnected: false,
  revision: 0,
  setJobs: (update) =>
    set((s) => ({
      jobs: typeof update === "function" ? update(s.jobs) : update,
    })),
}));

/** One app-wide stream, so completion refreshes selectors even away from Brain. */
export function connectModelManagerEvents() {
  const events = modelManager.events();
  let mounted = true;
  let previous: Record<string, string> = {};
  events.onmessage = (event) => {
    if (!mounted) return;
    try {
      const rows = (JSON.parse(event.data) as { jobs: ModelJob[] }).jobs;
      const changed = rows.some(
        (job) =>
          previous[job.id] &&
          previous[job.id] !== job.status &&
          !activeModelJob(job.status),
      );
      previous = Object.fromEntries(rows.map((row) => [row.id, row.status]));
      useModelManagerStore.setState((s) => ({
        jobs: rows,
        disconnected: false,
        revision: s.revision + (changed ? 1 : 0),
      }));
      if (changed) {
        void modelManager
          .inventory()
          .then(async () => {
            await useAiStore.getState().load();
            const models = await fetchModels();
            if (mounted) useChatStore.getState().setModels(models.models);
          })
          .catch(() => {});
      }
    } catch {
      useModelManagerStore.setState({ disconnected: true });
    }
  };
  events.onerror = () => {
    if (mounted) useModelManagerStore.setState({ disconnected: true });
  };
  events.addEventListener("unavailable", () => {
    if (mounted) useModelManagerStore.setState({ disconnected: true });
  });
  return () => {
    mounted = false;
    events.close();
  };
}
