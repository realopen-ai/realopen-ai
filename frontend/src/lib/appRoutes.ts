export type BrainTab = "memories" | "history" | "skills" | "tools";
export type WorkspaceSection =
  | "documents"
  | "templates"
  | "generated"
  | "assets"
  | "sandboxes"
  | "artifacts";

const brainTabs = new Set<BrainTab>(["memories", "history", "skills", "tools"]);

const workspaceSections = new Set<WorkspaceSection>([
  "artifacts",
  "documents",
  "templates",
  "generated",
  "assets",
  "sandboxes",
]);

export function isBrainRoute(pathname: string): boolean {
  return pathname === "/brain" || pathname.startsWith("/brain/");
}

export function isLearnRoute(pathname: string): boolean {
  return pathname === "/learn" || pathname.startsWith("/learn/");
}

export function isWorkspaceRoute(pathname: string): boolean {
  return pathname === "/workspace" || pathname.startsWith("/workspace/");
}

export function getBrainRoute(pathname: string): {
  tab: BrainTab;
  toolId: string | null;
} {
  const parts = pathname.split("/").filter(Boolean);
  const candidate = parts[1] as BrainTab | undefined;
  const tab = candidate && brainTabs.has(candidate) ? candidate : "memories";
  return {
    tab,
    toolId: tab === "tools" && parts[2] ? decodeURIComponent(parts[2]) : null,
  };
}

export function getWorkspaceSection(pathname: string): WorkspaceSection | null {
  const candidate = pathname.split("/").filter(Boolean)[1] as
    WorkspaceSection | undefined;
  return candidate && workspaceSections.has(candidate) ? candidate : null;
}

export function brainTabPath(tab: BrainTab): string {
  return tab === "memories" ? "/brain" : `/brain/${tab}`;
}

export function workspaceSectionPath(section: WorkspaceSection): string {
  return `/workspace/${section}`;
}
