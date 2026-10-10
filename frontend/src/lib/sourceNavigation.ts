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

export function artifactReadLocation(output: string): SourceLocation | null {
  try {
    const value = JSON.parse(output);
    if (
      typeof value.artifact_id !== "string" ||
      !Number.isSafeInteger(value.version) ||
      value.version < 1 ||
      typeof value.id !== "string"
    )
      return null;
    return {
      artifact: {
        artifact_id: value.artifact_id,
        version: value.version,
        section_id: value.id,
      },
      title: typeof value.title === "string" ? value.title : undefined,
    };
  } catch {
    return null;
  }
}
