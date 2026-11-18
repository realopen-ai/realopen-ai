import type { SourceLocation } from "./sourceNavigation";
import type { PinnedArtifact } from "../api/artifactsClient";
import type { LensImage } from "../api/lensClient";

export interface FilePreview {
  reportId: string;
  filename: string;
  downloadUrl: string;
  format: "pptx" | "pdf" | "docx" | "xlsx";
  artifactVersion?: PinnedArtifact;
}

export type SideActivity =
  | { kind: "quiz"; id: string; title: string; attemptId?: string }
  | { kind: "flashcards"; id: string; title: string }
  | { kind: "citation"; source: SourceLocation }
  | { kind: "file"; file: FilePreview }
  | { kind: "lens-results"; id: string; title: string; images: LensImage[] }
  | { kind: "lens-image"; id: string; title: string };

export function sideTabRouteKey({
  pathname,
  key,
}: {
  pathname: string;
  key: string;
}) {
  return `${pathname}:${key}`;
}

export function sideActivityKey(activity: SideActivity): string {
  switch (activity.kind) {
    case "lens-image":
    case "lens-results":
    case "quiz":
    case "flashcards":
      return `${activity.kind}:${activity.id}`;
    case "file":
      return `file:${activity.file.reportId}:${activity.file.format}`;
    case "citation": {
      const { artifact, documentId, page, chunk } = activity.source;
      return artifact
        ? `artifact:${artifact.artifact_id}:${artifact.version}:${artifact.section_id}`
        : `document:${documentId}:${page ?? ""}:${chunk ?? ""}`;
    }
  }
}
