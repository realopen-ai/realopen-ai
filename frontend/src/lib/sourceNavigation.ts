import type { ArtifactReference } from "../api/artifactsClient";

export interface SourceLocation {
  documentId?: string | null;
  artifact?: ArtifactReference | null;
  page?: number | null;
  chunk?: string | null;
  title?: string;
  excerpt?: string;
}

export function isNotebookSourceRoute(pathname: string, notebookId: string) {
  return pathname.replace(/\/$/, "") === `/learn/notebooks/${notebookId}`;
}

export function sourceLocationUrl(source: SourceLocation) {
  const params = new URLSearchParams();
  if (source.artifact) {
    params.set("version", String(source.artifact.version));
    params.set("section", source.artifact.section_id);
    return `/workspace/artifacts/${encodeURIComponent(source.artifact.artifact_id)}?${params}`;
  }
  if (!source.documentId) return null;
  params.set("document", source.documentId);
  if (source.page && Number.isSafeInteger(source.page) && source.page > 0)
    params.set("page", String(source.page));
  if (source.chunk) params.set("chunk", source.chunk);
  return `/workspace/documents?${params}`;
}

export function openNotebookSource(notebookId: string, source: SourceLocation) {
  window.dispatchEvent(
    new CustomEvent("notebook-source", { detail: { notebookId, source } }),
  );
}

export function artifactCitationLocations(output: string): SourceLocation[] {
  try {
    const value = JSON.parse(output);
    if (
      typeof value.artifact_id !== "string" ||
      !Number.isSafeInteger(value.version) ||
      value.version < 1
    )
      return [];
    const references = Array.isArray(value.source_references)
      ? value.source_references
      : [value];
    const seen = new Set<string>();
    const sources: SourceLocation[] = [];
    for (const ref of references) {
      if (typeof ref?.id !== "string" || !ref.id || seen.has(ref.id)) continue;
      seen.add(ref.id);
      sources.push({
        artifact: {
          artifact_id: value.artifact_id,
          version: value.version,
          section_id: ref.id,
        },
        title: typeof ref.title === "string" ? ref.title : undefined,
      });
    }
    return sources;
  } catch {
    return [];
  }
}
