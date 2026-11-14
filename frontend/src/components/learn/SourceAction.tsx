import { Link, useLocation } from "react-router-dom";
import { ExternalLink } from "lucide-react";
import { useT } from "@/store/settingsStore";
import { useNotebookStore } from "@/store/notebookStore";
import { useUIStore } from "@/store/uiStore";
import { cn } from "@/lib/utils";
import {
  openNotebookSource,
  isNotebookSourceRoute,
  sourceLocationUrl,
} from "@/lib/sourceNavigation";
import type { ArtifactReference } from "@/api/artifactsClient";

export function SourceAction({
  documentId,
  conversationId,
  page,
  chunk,
  artifact,
  excerpt,
  title,
  label,
  className,
}: {
  documentId?: string | null;
  conversationId?: string | null;
  page?: number | null;
  chunk?: string | null;
  artifact?: ArtifactReference | null;
  excerpt?: string;
  title?: string;
  label?: string;
  className?: string;
}) {
  const t = useT();
  const notebook = useNotebookStore((s) => s.notebook);
  const { pathname, key } = useLocation();
  const source = { documentId, artifact, page, chunk, excerpt, title };
  const previewHref = sourceLocationUrl(source);
  const href = previewHref ?? (conversationId ? `/${conversationId}` : null);
  return href ? (
    <Link
      to={href}
      onClick={(event) => {
        if (
          previewHref &&
          !event.defaultPrevented &&
          event.button === 0 &&
          !event.metaKey &&
          !event.ctrlKey &&
          !event.shiftKey &&
          !event.altKey
        ) {
          event.preventDefault();
          if (notebook && isNotebookSourceRoute(pathname, notebook.id))
            openNotebookSource(notebook.id, source);
          else
            useUIStore.getState().setCitationPreview({ source, routeKey: key });
        }
      }}
      className={cn(
        "inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-primary",
        className,
      )}
    >
      <ExternalLink className="size-3" />
      {label ?? t("learn.viewSource")}
      {page ? ` · ${page}` : ""}
    </Link>
  ) : null;
}
