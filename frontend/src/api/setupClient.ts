import { dbgError, createDebugLogger } from "@/lib/debug";

const log = createDebugLogger("setupClient");

// ─── Types ───────────────────────────────────────────────────────

export interface HardwareInfo {
  platform: string;
  platform_display: string;
  ram_gb: number;
  cpu_cores: number;
  cpu_name: string;
  gpu_type: string;
  gpu_vram_mb: number;
  gpu_vram_gb: number;
  gpu_name: string;
  recommended_profile: string;
  ollama_installed: boolean;
  ollama_running: boolean;
  docker_available: boolean;
}

export interface SetupProfile {
  description: string;
  label: string;
  engine: string;
  models: {
    id: string;
    type: string;
    role: string;
    description: string;
    size: string;
  }[];
}

export interface SetupModule {
  name: string;
  required: boolean;
  label: string;
  description: string;
  icon: string;
  estimated_size: string;
  minimum_requirements?: { ram: number; vram: number };
  availability: Record<string, boolean>;
  requirements_met: boolean;
  models: {
    id: string;
    type: string;
    role: string;
    description: string;
    size: string;
  }[];
}

export interface SetupStatus {
  setup_complete: boolean;
  profile: string | null;
}

export interface ApplySetupResponse {
  status: string;
  profile: string;
  enabled_modules: string[];
  models_to_pull: {
    id: string;
    role: string;
    description: string;
    size: string;
    module: string;
  }[];
}

export interface PullModelEvent {
  event: string;
  model?: string;
  module?: string;
  index?: number;
  total?: number;
  total_models?: number;
  status?: string;
  completed?: number;
  percent?: number;
  error?: string;
  reason?: string;
}

// ─── API Functions ────────────────────────────────────────────────

export async function fetchSetupStatus(): Promise<SetupStatus> {
  log("➡️  fetchSetupStatus  url=/api/setup/status");
  try {
    const res = await fetch("/api/setup/status");
    log(`   response  status=${res.status}  ok=${res.ok}`);
    if (res.ok) return await res.json();
    dbgError(`   ❌ fetchSetupStatus NOT OK  status=${res.status}`);
  } catch (err) {
    dbgError(`   ❌ fetchSetupStatus error: ${err}`);
  }
  return { setup_complete: false, profile: null };
}

export async function fetchHardwareInfo(): Promise<HardwareInfo | null> {
  log("➡️  fetchHardwareInfo  url=/api/setup/hardware");
  try {
    const res = await fetch("/api/setup/hardware");
    log(`   response  status=${res.status}  ok=${res.ok}`);
    if (res.ok) return await res.json();
    dbgError(`   ❌ fetchHardwareInfo NOT OK  status=${res.status}`);
  } catch (err) {
    dbgError(`   ❌ fetchHardwareInfo error: ${err}`);
  }
  return null;
}

export async function fetchSetupProfiles(): Promise<Record<
  string,
  SetupProfile
> | null> {
  log("➡️  fetchSetupProfiles  url=/api/setup/profiles");
  try {
    const res = await fetch("/api/setup/profiles");
    log(`   response  status=${res.status}  ok=${res.ok}`);
    if (res.ok) return await res.json();
    dbgError(`   ❌ fetchSetupProfiles NOT OK  status=${res.status}`);
  } catch (err) {
    dbgError(`   ❌ fetchSetupProfiles error: ${err}`);
  }
  return null;
}

export async function fetchSetupModules(): Promise<SetupModule[] | null> {
  log("➡️  fetchSetupModules  url=/api/setup/modules");
  try {
    const res = await fetch("/api/setup/modules");
    log(`   response  status=${res.status}  ok=${res.ok}`);
    if (res.ok) {
      const data = await res.json();
      return data.modules ?? [];
    }
    dbgError(`   ❌ fetchSetupModules NOT OK  status=${res.status}`);
  } catch (err) {
    dbgError(`   ❌ fetchSetupModules error: ${err}`);
  }
  return null;
}

export async function applySetup(
  profile: string,
  enabledModules: string[],
): Promise<ApplySetupResponse | null> {
  log(`➡️  applySetup  profile=${profile}  modules=${enabledModules}`);
  try {
    const res = await fetch("/api/setup/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile, enabled_modules: enabledModules }),
    });
    log(`   response  status=${res.status}  ok=${res.ok}`);
    if (res.ok) return await res.json();
    const err = await res.json().catch(() => ({}));
    dbgError(`   ❌ applySetup error: ${err.detail || res.status}`);
  } catch (err) {
    dbgError(`   ❌ applySetup error: ${err}`);
  }
  return null;
}

export async function pullSetupModels(
  profile: string,
  enabledModules: string[],
  onEvent: (event: PullModelEvent) => void,
): Promise<boolean> {
  log(`➡️  pullSetupModels  profile=${profile}`);
  try {
    const res = await fetch("/api/setup/pull-models", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile, enabled_modules: enabledModules }),
    });

    if (!res.ok || !res.body) {
      dbgError(`   ❌ pullSetupModels NOT OK  status=${res.status}`);
      return false;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        try {
          const event = JSON.parse(line.slice(6));
          onEvent(event);
        } catch {
          // Skip malformed JSON
        }
      }
    }

    return true;
  } catch (err) {
    dbgError(`   ❌ pullSetupModels error: ${err}`);
    return false;
  }
}

export async function completeSetup(): Promise<boolean> {
  log("➡️  completeSetup  url=/api/setup/complete");
  try {
    const res = await fetch("/api/setup/complete", {
      method: "POST",
    });
    log(`   response  status=${res.status}  ok=${res.ok}`);
    return res.ok;
  } catch (err) {
    dbgError(`   ❌ completeSetup error: ${err}`);
  }
  return false;
}
