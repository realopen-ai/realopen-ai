import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeft,
  FileText,
  Loader2,
  History,
  Download,
  Eye,
  Save,
} from "lucide-react";
import {
  artifactsApi,
  artifactSelectionSearch,
  type Artifact,
  type ArtifactDetail,
} from "@/api/artifactsClient";
import { Button } from "@/components/ui/button";
import { CardMarkdown } from "@/components/learn/CardMarkdown";
import {
  FileViewerModal,
  type ViewerFormat,
} from "@/components/chat/FileViewerModal";
import { useT } from "@/store/settingsStore";

export function ArtifactsSection() {
  const { artifactId } = useParams();
  return artifactId ? (
    <ArtifactEditor key={artifactId} id={artifactId} />
  ) : (
    <ArtifactList />
  );
}
function ArtifactList() {
  const t = useT();
  const [items, setItems] = useState<Artifact[]>([]);
  const [next, setNext] = useState<number | null>(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function load(offset: number, signal?: AbortSignal) {
    setBusy(true);
    setError("");
    try {
      const result = await artifactsApi.list(offset, signal);
      setItems((old) =>
        offset === 0 ? result.items : [...old, ...result.items],
      );
      setNext(result.next_offset);
    } catch (e) {
      if (!signal?.aborted) setError(String(e));
    } finally {
      if (!signal?.aborted) setBusy(false);
    }
  }
  useEffect(() => {
    const controller = new AbortController();
    void load(0, controller.signal);
    return () => controller.abort();
  }, []);
  return (
    <div className="h-full overflow-auto p-6">
      <div className="grid gap-3 sm:grid-cols-2">
        {items.map((file) => (
          <Link
            key={file.id}
            to={`/workspace/artifacts/${file.id}`}
            className="rounded-xl border border-border bg-card p-4 hover:border-primary/50"
          >
            <div className="flex items-center gap-3">
              <FileText className="h-5 w-5 text-primary" />
              <span className="truncate font-medium">{file.title}</span>
            </div>
            <div className="mt-3 text-xs text-muted-foreground">
              {t(file.editable ? "artifacts.editable" : "artifacts.readOnly")} ·{" "}
              {t("artifacts.version", { number: file.version })}
            </div>
          </Link>
        ))}
      </div>
      {!busy && !items.length && (
        <p className="text-muted-foreground">{t("artifacts.empty")}</p>
      )}
      {error && (
        <p role="alert" className="text-danger">
          {error}
        </p>
      )}
      {busy && <Loader2 className="m-4 animate-spin" />}
      {next !== null && !busy && (
        <Button variant="outline" className="mt-4" onClick={() => load(next)}>
          {t("artifacts.more")}
        </Button>
      )}
    </div>
  );
}
function ArtifactEditor({ id }: { id: string }) {
  const t = useT(),
    navigate = useNavigate(),
    location = useLocation();
  const query = new URLSearchParams(location.search);
  const requestedVersion = Number(query.get("version"));
  const version = requestedVersion > 0 ? requestedVersion : undefined;
  const [detail, setDetail] = useState<ArtifactDetail | null>(null),
    [content, setContent] = useState(""),
    [original, setOriginal] = useState(""),
    [edit, setEdit] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [viewer, setViewer] = useState<{
      id: string;
      format: ViewerFormat;
      url: string;
    } | null>(null),
    [restore, setRestore] = useState(false);
  const requestedSection = query.get("section");
  const section = detail?.outline.sections.some(
    (s) => s.id === requestedSection,
  )
    ? requestedSection!
    : (detail?.outline.sections[0]?.id ?? "");
  const select = (selection: { version?: number; section?: string }) =>
    navigate(
      { search: artifactSelectionSearch(location.search, selection) },
      { replace: true },
    );
  const dirty = content !== original;
  useEffect(() => {
    const controller = new AbortController();
    setError("");
    setBusy(true);
    artifactsApi
      .detail(id, version, controller.signal)
      .then((result) => {
        setDetail(result);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(String(e));
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [id, version]);
  useEffect(() => {
    if (!detail || !section) return;
    const controller = new AbortController();
    setBusy(true);
    setError("");
    setContent("");
    setOriginal("");
    (async () => {
      let text = "",
        offset = 0;
      do {
        const part = await artifactsApi.read(
          id,
          detail.selected_version,
          section,
          offset,
          controller.signal,
        );
        text += part.content;
        if (part.next_offset === null) break;
        offset = part.next_offset;
      } while (text.length < 2_000_000);
      if (!controller.signal.aborted) {
        setContent(text);
        setOriginal(text);
      }
    })()
      .catch((e) => {
        if (!controller.signal.aborted) setError(String(e));
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [id, detail, section]);
  useEffect(() => {
    if (!dirty) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);
  const canLeave = () => !dirty || window.confirm(t("artifacts.discard"));
  async function action(run: () => Promise<unknown>, followLatest = true) {
    setBusy(true);
    setError("");
    try {
      await run();
      const result = await artifactsApi.detail(
        id,
        followLatest ? undefined : detail?.selected_version,
      );
      setOriginal(content);
      setEdit(false);
      setDetail(result);
      select({ version: result.selected_version });
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  if (!detail)
    return (
      <div className="p-6">
        {busy ? (
          <Loader2 className="animate-spin" />
        ) : (
          <p role="alert" className="text-danger">
            {error}
          </p>
        )}
      </div>
    );
  const selected = detail.outline.sections.find((s) => s.id === section);
  return (
    <div className="h-full overflow-auto p-6 space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <Button
          variant="ghost"
          size="icon"
          aria-label={t("artifacts.back")}
          onClick={() => {
            if (canLeave()) navigate("/workspace/artifacts");
          }}
        >
          <ArrowLeft />
        </Button>
        <h2 className="min-w-0 flex-1 truncate text-xl font-semibold">
          {detail.title}
        </h2>
        <label className="flex items-center gap-2 text-sm">
          <History className="h-4 w-4" />
          <select
            className="rounded-md border border-border bg-background p-2"
            disabled={busy}
            value={detail.selected_version}
            onChange={(e) => {
              if (canLeave()) {
                setEdit(false);
                select({ version: Number(e.target.value) });
              }
            }}
          >
            {detail.versions.map((v) => (
              <option key={v.number} value={v.number}>
                {t("artifacts.version", { number: v.number })} ·{" "}
                {new Date(v.created_at).toLocaleDateString()}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <select
          aria-label={t("artifacts.sections")}
          className="max-w-full rounded-md border border-border bg-background p-2"
          disabled={busy}
          value={section}
          onChange={(e) => {
            if (canLeave()) {
              setEdit(false);
              select({ section: e.target.value });
            }
          }}
        >
          {detail.outline.sections.map((s) => (
            <option key={s.id} value={s.id}>
              {s.title}
            </option>
          ))}
        </select>
        {detail.editable && detail.selected_version === detail.version && (
          <Button
            variant="outline"
            disabled={busy}
            onClick={() => {
              if (edit && dirty && !canLeave()) return;
              setContent(original);
              setEdit(!edit);
            }}
          >
            {t(edit ? "artifacts.preview" : "artifacts.edit")}
          </Button>
        )}
        {edit && (
          <Button
            disabled={busy || !dirty}
            onClick={() =>
              action(() =>
                artifactsApi.update(id, detail.version, section, content),
              )
            }
          >
            <Save className="h-4 w-4" />
            {t("artifacts.save")}
          </Button>
        )}
        {detail.editable && detail.selected_version !== detail.version && (
          <Button
            variant="outline"
            disabled={busy}
            onClick={() => setRestore(true)}
          >
            {t("artifacts.restore")}
          </Button>
        )}
        {detail.outputs.map((fmt) => (
          <a
            key={fmt}
            className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-sm text-primary hover:underline"
            href={`/api/artifacts/${id}/download/${fmt}?version=${detail.selected_version}`}
          >
            <Download className="h-4 w-4" />
            {fmt === "original" ? t("artifacts.original") : fmt.toUpperCase()}
          </a>
        ))}
        {Object.entries(detail.previews).map(([fmt, report]) => (
          <Button
            key={fmt}
            variant="ghost"
            size="sm"
            disabled={busy}
            onClick={() =>
              setViewer({
                id: report,
                format: fmt as ViewerFormat,
                url: `/api/artifacts/${id}/download/${fmt}?version=${detail.selected_version}`,
              })
            }
          >
            <Eye className="h-4 w-4" />
            {t("artifacts.preview")} {fmt.toUpperCase()}
          </Button>
        ))}
        {detail.export_formats
          .filter((fmt) => !detail.outputs.includes(fmt))
          .map((fmt) => (
            <Button
              key={fmt}
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={() =>
                action(
                  () => artifactsApi.export(id, detail.selected_version, fmt),
                  false,
                )
              }
            >
              {t("artifacts.export")} {fmt.toUpperCase()}
            </Button>
          ))}
        {detail.document_id && (
          <Link
            className="text-sm text-muted-foreground hover:text-primary"
            to={`/workspace/documents?document=${encodeURIComponent(detail.document_id)}${selected?.page ? `&page=${selected.page}` : ""}`}
          >
            {t("learn.viewSource")}
          </Link>
        )}
        {busy && <Loader2 className="h-4 w-4 animate-spin" />}
      </div>
      <p className="text-xs text-muted-foreground">
        {t(detail.editable ? "artifacts.safeEdits" : "artifacts.readOnly")}
        {selected?.method === "ocr" && ` · ${t("artifacts.ocr")}`}
      </p>
      {detail.outline.warnings.map((w) => (
        <p key={w} className="text-xs text-amber-600">
          {w}
        </p>
      ))}
      {error && (
        <p role="alert" className="text-danger">
          {error}
        </p>
      )}
      {edit ? (
        <textarea
          dir="ltr"
          className="min-h-[400px] w-full rounded-xl border border-border bg-card p-5 font-mono text-sm"
          value={content}
          disabled={busy}
          onChange={(e) => setContent(e.target.value)}
          maxLength={2_000_000}
        />
      ) : (
        <div className="rounded-xl border border-border bg-card p-5" dir="auto">
          <CardMarkdown
            text={
              detail.kind === "excel"
                ? `\`\`\`json\n${content}\n\`\`\``
                : content
            }
          />
        </div>
      )}
      {restore && (
        <div
          role="dialog"
          aria-modal="true"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
        >
          <div className="max-w-md rounded-xl border border-border bg-card p-6 space-y-4">
            <p>{t("artifacts.restoreConfirm")}</p>
            <div className="flex gap-2">
              <Button
                variant="outline"
                disabled={busy}
                onClick={() => setRestore(false)}
              >
                {t("artifacts.cancel")}
              </Button>
              <Button
                disabled={busy}
                onClick={() => {
                  setRestore(false);
                  void action(() =>
                    artifactsApi.restore(
                      id,
                      detail.version,
                      detail.selected_version,
                    ),
                  );
                }}
              >
                {t("artifacts.restore")}
              </Button>
            </div>
          </div>
        </div>
      )}
      {viewer && (
        <FileViewerModal
          reportId={viewer.id}
          filename={detail.title}
          downloadUrl={viewer.url}
          format={viewer.format}
          onClose={() => setViewer(null)}
        />
      )}
    </div>
  );
}
