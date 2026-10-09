import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ExternalLink, Loader2 } from "lucide-react";
import {
  artifactsApi,
  type ArtifactDetail,
  type ArtifactRead,
} from "@/api/artifactsClient";
import { useT } from "@/store/settingsStore";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { CardMarkdown } from "./CardMarkdown";
import { fieldClass } from "./LearnDialogs";

export function NotebookSourcePreview({
  id,
  title,
  onClose,
}: {
  id: string;
  title: string;
  onClose: () => void;
}) {
  const t = useT();
  const [detail, setDetail] = useState<ArtifactDetail | null>(null);
  const [section, setSection] = useState("");
  const [offset, setOffset] = useState(0);
  const [read, setRead] = useState<ArtifactRead | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    artifactsApi
      .detail(id, undefined, controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        setDetail(value);
        setSection(value.outline.sections[0]?.id ?? "");
        if (!value.outline.sections.length) setLoading(false);
      })
      .catch((e) => {
        if (!controller.signal.aborted) {
          setError(e.message);
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [id]);
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
  return (
    <Dialog open onOpenChange={onClose}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>
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
        <DialogFooter>
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
            <Link
              to={`/workspace/artifacts/${detail?.id ?? id}${detail && section ? `?version=${detail.selected_version}&section=${encodeURIComponent(section)}` : ""}`}
            >
              <ExternalLink className="size-4" />
              {t("notebooks.openSource")}
            </Link>
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
