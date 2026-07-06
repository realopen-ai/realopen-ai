/** API client for the dependency management system. */

export interface DependencyInfo {
  name: string;
  display_name: string;
  description: string;
  category: string;
  kind: string; // "system" | "pip"
  installed: boolean;
  in_overlay: boolean; // in the /opt/optional overlay volume
  version: string | null;
  install_size: string;
  enables: string[];
  available: boolean;
  install_cmd: string | null;
  distro: string;
  persistence: string; // "overlay-volume" | "system" | "none"
}

export interface InstallEvent {
  stage:
    | "checking_sudo"
    | "updating"
    | "downloading"
    | "installing"
    | "extracting"
    | "linking"
    | "uninstalling"
    | "done"
    | "error";
  output?: string;
  error?: string;
  exit_code?: number;
  version?: string;
  manual_command?: string;
  recent_output?: string;
}

/** Fetch the list of all dependencies with their status. */
export async function fetchDependencies(): Promise<DependencyInfo[]> {
  try {
    const res = await fetch("/api/deps");
    if (res.ok) {
      const data = await res.json();
      return data.dependencies ?? [];
    }
  } catch {
    /* ignore */
  }
  return [];
}

/** Install a dependency via SSE stream.
 *  Calls onEvent for each progress event. Returns the final event.
 */
export async function installDependency(
  name: string,
  onEvent: (event: InstallEvent) => void,
): Promise<InstallEvent> {
  const res = await fetch(`/api/deps/${name}/install`, { method: "POST" });

  if (!res.ok || !res.body) {
    throw new Error(`HTTP ${res.status}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalEvent: InstallEvent = { stage: "error", error: "No response" };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";

    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      const data = line.slice(6).trim();
      if (!data) continue;
      try {
        const event = JSON.parse(data) as InstallEvent;
        onEvent(event);
        finalEvent = event;
        if (event.stage === "done" || event.stage === "error") {
          return event;
        }
      } catch {
        /* skip malformed JSON */
      }
    }
  }

  return finalEvent;
}

/** Uninstall a dependency via SSE stream.
 *  Calls onEvent for each progress event. Returns the final event.
 */
export async function uninstallDependency(
  name: string,
  onEvent: (event: InstallEvent) => void,
): Promise<InstallEvent> {
  const res = await fetch(`/api/deps/${name}/uninstall`, { method: "DELETE" });

  if (!res.ok || !res.body) {
    throw new Error(`HTTP ${res.status}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalEvent: InstallEvent = { stage: "error", error: "No response" };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";

    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      const data = line.slice(6).trim();
      if (!data) continue;
      try {
        const event = JSON.parse(data) as InstallEvent;
        onEvent(event);
        finalEvent = event;
        if (event.stage === "done" || event.stage === "error") {
          return event;
        }
      } catch {
        /* skip malformed JSON */
      }
    }
  }

  return finalEvent;
}
