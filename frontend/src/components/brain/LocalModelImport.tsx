import { useEffect, useRef, useState } from "react";
import { FolderOpen, Loader2, Upload } from "lucide-react";
import {
  modelManager,
  type CatalogModel,
  type LocalImportOptions,
  type ModelInventory,
  type ModelJob,
} from "@/api/modelManagerClient";
import { localModelFiles } from "@/lib/localModels";
import { modelBytes, modelJobProgress } from "@/lib/modelManager";
import { ModelDetails } from "@/components/brain/ModelDetails";
import { useT } from "@/store/settingsStore";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const input =
  "h-10 min-w-0 rounded-xl border border-border bg-background px-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring/50";

export function LocalModelImport({
  options,
  runtimes,
  onStarted,
}: {
  options: LocalImportOptions;
  runtimes: ModelInventory["runtimes"];
  onStarted: (job: ModelJob) => void;
}) {
  const t = useT();
  const [mode, setMode] = useState<"files" | "path">("files");
  const [path, setPath] = useState("");
  const [format, setFormat] = useState("auto");
  const [selection, setSelection] = useState<{ file: File; path: string }[]>(
    [],
  );
  const [ignored, setIgnored] = useState(0);
  const [detail, setDetail] = useState<CatalogModel | null>(null);
  const [uploadId, setUploadId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const controller = useRef<AbortController | null>(null);
  const filesInput = useRef<HTMLInputElement | null>(null);
  const folderInput = useRef<HTMLInputElement | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  const total = selection.reduce((sum, item) => sum + item.file.size, 0);

  function select(files: FileList | null) {
    if (!files) return;
    setError(null);
    try {
      const result = localModelFiles(Array.from(files), options);
      setSelection(result.files);
      setIgnored(result.ignored);
    } catch (e) {
      setSelection([]);
      setError(
        t(`models.local.${(e as Error).message}` as Parameters<typeof t>[0]),
      );
    }
  }
  async function inspect() {
    const abort = new AbortController();
    controller.current = abort;
    setError(null);
    setBusy(true);
    setProgress(0);
    let staged: string | null = null;
    try {
      if (mode === "path") {
        setDetail(
          await modelManager.inspectLocalPath(path, format, abort.signal),
        );
      } else {
        const session = await modelManager.beginLocalUpload(
          selection[0]?.file.webkitRelativePath?.split("/")[0] ||
            selection[0]?.file.name ||
            "Local model",
          selection.map((item) => ({ path: item.path, size: item.file.size })),
          format,
          abort.signal,
        );
        staged = session.id;
        let complete = 0;
        for (const item of selection) {
          let offset = 0;
          while (offset < item.file.size) {
            const chunk = item.file.slice(offset, offset + session.chunk_size);
            const result = await modelManager.uploadLocalChunk(
              session.id,
              item.path,
              offset,
              chunk,
              abort.signal,
            );
            if (result.offset !== offset + chunk.size)
              throw new Error(t("models.local.uploadMismatch"));
            offset = result.offset;
            setProgress(complete + offset);
          }
          complete += item.file.size;
        }
        setDetail(
          await modelManager.finishLocalUpload(session.id, abort.signal),
        );
        setUploadId(session.id);
        staged = null;
      }
    } catch (e) {
      if (!(e instanceof DOMException && e.name === "AbortError"))
        setError((e as Error).message);
      if (staged) await modelManager.discardLocalUpload(staged).catch(() => {});
    } finally {
      setBusy(false);
      controller.current = null;
    }
  }
  async function reset() {
    if (uploadId) {
      setBusy(true);
      try {
        await modelManager.discardLocalUpload(uploadId);
        setUploadId(null);
        setDetail(null);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setBusy(false);
      }
    } else setDetail(null);
  }

  return (
    <section className="space-y-4">
      <p className="text-sm text-muted-foreground">
        {t("models.local.description")}
      </p>
      {error && (
        <p
          role="alert"
          className="rounded-xl border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive"
        >
          {error}
        </p>
      )}
      {!detail && (
        <>
          <div className="flex gap-2">
            {(["files", "path"] as const).map((value) => (
              <Button
                key={value}
                variant={mode === value ? "secondary" : "ghost"}
                disabled={busy}
                onClick={() => {
                  setMode(value);
                  setError(null);
                }}
              >
                {t(`models.local.${value}`)}
              </Button>
            ))}
          </div>
          {mode === "files" ? (
            <div className="rounded-2xl border border-dashed border-border p-5 space-y-3">
              <input
                type="file"
                multiple
                hidden
                ref={filesInput}
                accept={
                  options.extensions.join(",") + ",.json,.model,.txt,.jinja"
                }
                onChange={(e) => {
                  select(e.target.files);
                  e.target.value = "";
                }}
              />
              <input
                type="file"
                multiple
                hidden
                ref={(node) => {
                  folderInput.current = node;
                  if (node) node.setAttribute("webkitdirectory", "");
                }}
                onChange={(e) => {
                  select(e.target.files);
                  e.target.value = "";
                }}
              />
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="outline"
                  disabled={busy}
                  onClick={() => filesInput.current?.click()}
                >
                  <Upload className="h-4 w-4" />
                  {t("models.local.chooseFiles")}
                </Button>
                <Button
                  variant="outline"
                  disabled={busy}
                  onClick={() => folderInput.current?.click()}
                >
                  <FolderOpen className="h-4 w-4" />
                  {t("models.local.chooseFolder")}
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">
                {t("models.local.filesHelp")}
              </p>
              {selection.length > 0 && (
                <>
                  <p className="text-sm">
                    {selection.length} {t("models.files").toLowerCase()} ·{" "}
                    {modelBytes(total)}
                  </p>
                  <ul className="max-h-36 overflow-y-auto space-y-1 text-xs text-muted-foreground">
                    {selection.map((item) => (
                      <li key={item.path} className="break-all">
                        {item.path} · {modelBytes(item.file.size)}
                      </li>
                    ))}
                  </ul>
                  {ignored > 0 && (
                    <p className="text-xs text-muted-foreground">
                      {ignored} · {t("models.local.ignored")}
                    </p>
                  )}
                </>
              )}
            </div>
          ) : (
            <label className="block space-y-2 text-sm">
              {t("models.local.hostPath")}
              <input
                className={cn(input, "block w-full")}
                value={path}
                disabled={busy}
                onChange={(e) => setPath(e.target.value)}
                placeholder={t("models.local.pathPlaceholder")}
              />
              <span className="block text-xs text-muted-foreground">
                {t("models.local.pathHelp")}
              </span>
            </label>
          )}
          <label className="block space-y-2 text-xs text-muted-foreground">
            {t("models.local.format")}
            <select
              className={cn(input, "block w-full sm:max-w-xs text-foreground")}
              value={format}
              disabled={busy}
              onChange={(e) => setFormat(e.target.value)}
            >
              <option value="auto">{t("models.local.auto")}</option>
              <option value="safetensors">Safetensors</option>
              <option value="mlx">MLX</option>
            </select>
          </label>
          {busy && mode === "files" && (
            <div role="status" className="space-y-2">
              <div
                role="progressbar"
                aria-label={t("models.local.uploading")}
                aria-valuenow={modelJobProgress(progress, total) ?? undefined}
                className="h-1.5 overflow-hidden rounded-full bg-muted"
              >
                <div
                  className="h-full bg-primary transition-[width]"
                  style={{
                    width: `${modelJobProgress(progress, total) ?? 0}%`,
                  }}
                />
              </div>
              <p className="text-xs text-muted-foreground">
                {t("models.local.uploading")} · {modelBytes(progress)} /{" "}
                {modelBytes(total)}
              </p>
            </div>
          )}
          <div className="flex gap-2">
            <Button
              disabled={
                busy ||
                (mode === "path" ? !path.trim() : selection.length === 0)
              }
              onClick={() => void inspect()}
            >
              {busy && <Loader2 className="h-4 w-4 animate-spin" />}
              {t(
                mode === "path"
                  ? "models.local.inspectPath"
                  : "models.local.uploadInspect",
              )}
            </Button>
            {busy && (
              <Button
                variant="ghost"
                onClick={() => controller.current?.abort()}
              >
                {t("models.cancel")}
              </Button>
            )}
          </div>
        </>
      )}
      {detail && (
        <ModelDetails
          key={detail.id}
          detail={detail}
          runtimes={runtimes}
          onStarted={onStarted}
          onClose={() => void reset()}
        />
      )}
    </section>
  );
}
