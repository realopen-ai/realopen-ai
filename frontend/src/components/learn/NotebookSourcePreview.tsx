import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ExternalLink, Loader2, X } from "lucide-react";
import {
  artifactsApi,
  type ArtifactDetail,
  type ArtifactRead,
  type ArtifactReference,
} from "@/api/artifactsClient";
import { useT } from "@/store/settingsStore";
import { Button } from "@/components/ui/button";
import { sourceLocationUrl } from "@/lib/sourceNavigation";
import { documentPageUrl, getDocumentPages } from "@/api/documentsClient";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { CardMarkdown } from "./CardMarkdown";
import { fieldClass } from "./LearnDialogs";

export function NotebookSourcePreview({
  id,
  title,
  onClose,
  location,
  embedded = false,
}: {
  id: string;
  title: string;
  onClose: () => void;
  location?: ArtifactReference;
  embedded?: boolean;
}) {
  const t = useT();
  const [detail, setDetail] = useState<ArtifactDetail | null>(null);
  const [section, setSection] = useState("");
  const [offset, setOffset] = useState(0);
  const [read, setRead] = useState<ArtifactRead | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [image, setImage] = useState<{ section: string; url: string } | null>(
    null,
  );
  useEffect(() => {
    const controller = new AbortController();
    artifactsApi
      .detail(id, location?.version, controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        setDetail(value);
        if (
          location?.section_id &&
          !value.outline.sections.some(
            (item) => item.id === location.section_id,
          )
        ) {
          setError(t("notebooks.noReadableContent"));
          setLoading(false);
          return;
        }
        setSection(
          value.outline.sections.find(
            (item) => item.id === location?.section_id,
          )?.id ??
            value.outline.sections[0]?.id ??
            "",
        );
        if (!value.outline.sections.length) setLoading(false);
      })
      .catch((e) => {
        if (!controller.signal.aborted) {
          setError(e.message);
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [id, location?.version, location?.section_id]);
  useEffect(() => {
    if (!detail || !section) return;
    const controller = new AbortController();
    setLoading(true);
    setRead(null);
    setError("");
    artifactsApi
      .read(
        detail.id,
        detail.selected_version,
        section,
        offset,
        controller.signal,
      )
      .then((value) => {
        if (!controller.signal.aborted) setRead(value);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [detail, section, offset]);
  const selected = detail?.outline.sections.find((item) => item.id === section);
  const slide = selected?.slide;
  const page = selected?.page;
  const presentationId =
    detail?.kind === "presentation" ? detail.previews.pptx : undefined;
  const documentId = detail?.document_id;
  useEffect(() => {
    setImage(null);
    if (!embedded || !section) return;
    const controller = new AbortController();
    const showImage = (url: string) => {
      if (!controller.signal.aborted) setImage({ section, url });
    };
    if (presentationId && slide) {
      void fetch(
        `/api/reports/${encodeURIComponent(presentationId)}/slides?format=pptx`,
        {
          signal: controller.signal,
        },
      )
        .then(async (response) => {
          if (!response.ok) return;
          const manifest: { slides: { index: number; url: string }[] } =
            await response.json();
          const match = manifest.slides.find((item) => item.index === slide);
          if (match) showImage(match.url);
        })
        .catch(() => {});
    } else if (documentId && (slide || page)) {
      const number = (slide ?? page)!;
      void getDocumentPages(documentId, controller.signal).then((manifest) => {
        if (manifest && number <= manifest.count)
          showImage(
            documentPageUrl(documentId, number, "full", manifest.source_mtime),
          );
      });
    }
    return () => controller.abort();
  }, [embedded, section, presentationId, slide, page, documentId]);
  const href =
    sourceLocationUrl({
      artifact:
        section && detail
          ? {
              artifact_id: id,
              version: detail.selected_version,
              section_id: section,
            }
          : location,
    }) ?? `/workspace/artifacts/${encodeURIComponent(id)}`;
  const content = (
    <>
      {embedded ? (
        <header className="flex items-center gap-3">
          <h2 dir="auto" className="font-medium truncate flex-1 min-w-0">
            {detail?.title ?? title}
          </h2>
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onClose}
            aria-label={t("common.close")}
          >
            <X className="size-4" />
          </Button>
        </header>
      ) : (
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>
      )}
      {detail && (
        <p className="text-xs text-muted-foreground">
          {t("artifacts.version", { number: detail.selected_version })}
        </p>
      )}
      {image?.section === section && (
        <img
          src={image.url}
          alt={selected?.title ?? title}
          className="w-full rounded-lg border border-border object-contain"
          onError={() => setImage(null)}
        />
      )}
      {detail && (
        <select
          aria-label={t("artifacts.sections")}
          className={fieldClass}
          value={section}
          onChange={(e) => {
            setSection(e.target.value);
            setOffset(0);
          }}
        >
          {detail.outline.sections.map((item) => (
            <option key={item.id} value={item.id}>
              {item.title}
            </option>
          ))}
        </select>
      )}
      <div className="max-h-[60vh] overflow-y-auto" aria-live="polite">
        {error ? (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        ) : loading ? (
          <Loader2 className="size-5 animate-spin" />
        ) : read ? (
          <CardMarkdown
            text={
              detail?.kind === "excel"
                ? `\`\`\`json\n${read.content}\n\`\`\``
                : read.content
            }
          />
        ) : (
          <p className="text-sm text-muted-foreground">
            {t("notebooks.noReadableContent")}
          </p>
        )}
      </div>
      <div className="flex flex-wrap justify-end gap-2">
        {offset > 0 && (
          <Button variant="outline" onClick={() => setOffset(0)}>
            {t("notebooks.startSection")}
          </Button>
        )}
        {read?.next_offset != null && (
          <Button
            variant="outline"
            onClick={() => setOffset(read.next_offset!)}
          >
            {t("notebooks.continueReading")}
          </Button>
        )}
        <Button asChild variant="outline">
          <Link to={href} onClick={onClose}>
            <ExternalLink className="size-4" />
            {t("sources.openFullFile")}
          </Link>
        </Button>
      </div>
    </>
  );
  return embedded ? (
    <div className="flex h-full min-h-0 flex-col gap-3 overflow-y-auto p-4">
      {content}
    </div>
  ) : (
    <Dialog open onOpenChange={onClose}>
      <DialogContent className="max-w-3xl">{content}</DialogContent>
    </Dialog>
  );
}
