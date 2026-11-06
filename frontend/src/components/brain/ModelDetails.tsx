import { useEffect, useState } from "react";
import { Download, ExternalLink, Loader2, X } from "lucide-react";
import {
  modelManager,
  type CatalogModel,
  type ModelInventory,
  type ModelJob,
  type ModelImportConfiguration,
} from "@/api/modelManagerClient";
import { modelBytes, modelInstallName } from "@/lib/modelManager";
import { useT } from "@/store/settingsStore";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const input =
  "h-10 rounded-xl border border-border bg-background px-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring/50 min-w-0";

export function ModelDetails({
  detail,
  runtimes,
  onStarted,
  onClose,
}: {
  detail: CatalogModel;
  runtimes: ModelInventory["runtimes"];
  onStarted: (job: ModelJob) => void;
  onClose: () => void;
}) {
  const t = useT();
  const [runtime, setRuntime] = useState(
    runtimes.find((r) => r.available)?.id ?? runtimes[0]?.id ?? "",
  );
  const first =
    detail.variants.find(
      (v) =>
        v.id === detail.requested_variant ||
        v.files.some((f) => f.path === detail.requested_variant),
    ) ??
    detail.variants.find((v) => v.compatibility[runtime]?.supported) ??
    detail.variants[0];
  const [variant, setVariant] = useState(first?.id ?? "");
  const local = detail.catalog === "local";
  const base = local
    ? detail.title
        .replace(/[^A-Za-z0-9_.-]/g, "-")
        .replace(/^[^A-Za-z0-9]+/, "") || "local-model"
    : detail.id;
  const [name, setName] = useState(
    first ? modelInstallName(detail.catalog, base, first.id) : "",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const selected = detail.variants.find((v) => v.id === variant);
  const compatibility = selected?.compatibility[runtime];
  const configuration = selected?.configurations?.[runtime];
  useEffect(() => {
    if (!runtimes.some((r) => r.id === runtime))
      setRuntime(runtimes[0]?.id ?? "");
  }, [runtimes, runtime]);

  return (
    <article className="rounded-2xl border border-border bg-muted/20 p-5 space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="break-all text-base font-medium">{detail.title}</h3>
          {detail.url && (
            <a
              href={detail.url}
              target="_blank"
              rel="noreferrer"
              className="mt-1 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-primary"
            >
              {detail.catalog}
              <ExternalLink className="h-3 w-3" />
            </a>
          )}
        </div>
        <Button
          variant="ghost"
          size="sm"
          aria-label={t("models.close")}
          disabled={busy}
          onClick={onClose}
        >
          <X className="h-4 w-4" />
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1.5 text-xs text-muted-foreground">
          {t("models.variant")}
          <select
            className={cn(input, "block w-full text-foreground")}
            value={variant}
            disabled={busy}
            onChange={(e) => {
              setVariant(e.target.value);
              setName(modelInstallName(detail.catalog, base, e.target.value));
            }}
          >
            {detail.variants.map((v) => (
              <option key={v.id} value={v.id}>
                {v.label} · {modelBytes(v.size)}
              </option>
            ))}
          </select>
        </label>
        <label className="space-y-1.5 text-xs text-muted-foreground">
          {t("models.runtime")}
          <select
            className={cn(input, "block w-full text-foreground")}
            value={runtime}
            disabled={busy}
            onChange={(e) => setRuntime(e.target.value)}
          >
            {runtimes.map((r) => (
              <option key={r.id} value={r.id}>
                {r.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      {detail.variants.length === 0 && (
        <p className="text-sm text-muted-foreground">
          {t("models.noVariants")}
        </p>
      )}
      {selected && (
        <>
          <p className="text-xs text-muted-foreground">
            {selected.format.toUpperCase()} · {modelBytes(selected.size)}
            {selected.quantization && ` · ${selected.quantization}`}
            {detail.license && ` · ${detail.license}`}
          </p>
          <p
            className={cn(
              "text-xs",
              compatibility?.supported
                ? "text-muted-foreground"
                : "text-amber-500",
            )}
          >
            {compatibility?.reason ?? t("models.incompatible")}
          </p>
          <p className="text-xs text-muted-foreground">
            {t(local ? "models.local.sourceHelp" : "models.diskHelp")}
          </p>
          <label className="block space-y-1.5 text-xs text-muted-foreground">
            {t("models.installName")}
            <input
              className={cn(input, "block w-full text-foreground")}
              value={name}
              maxLength={200}
              disabled={busy}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          {selected.files.length > 0 && (
            <details className="text-xs text-muted-foreground">
              <summary className="cursor-pointer">
                {t("models.files")} ({selected.files.length})
              </summary>
              <ul className="mt-2 max-h-40 overflow-y-auto space-y-1">
                {selected.files.map((file) => (
                  <li key={file.path} className="break-all">
                    {file.path} · {modelBytes(file.size)}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {configuration && (
            <ModelConfiguration configuration={configuration} />
          )}
          <Button
            disabled={
              busy ||
              !compatibility?.supported ||
              !name.trim() ||
              !runtimes.find((r) => r.id === runtime)?.available
            }
            onClick={() => {
              setBusy(true);
              setError(null);
              void modelManager
                .install({
                  catalog: detail.catalog,
                  model: detail.id,
                  variant,
                  runtime,
                  name,
                  revision: detail.revision,
                })
                .then(onStarted)
                .catch((e: Error) => setError(e.message))
                .finally(() => setBusy(false));
            }}
          >
            {busy ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Download className="h-4 w-4" />
            )}
            {t(local ? "models.local.import" : "models.download")}
          </Button>
        </>
      )}
    </article>
  );
}

export function ModelConfiguration({
  configuration,
}: {
  configuration: ModelImportConfiguration;
}) {
  const t = useT();
  return (
    <details className="text-xs text-muted-foreground">
      <summary className="cursor-pointer">
        {t("models.local.configuration")}
      </summary>
      <pre className="mt-2 overflow-x-auto rounded-lg bg-background p-3 whitespace-pre-wrap break-all">
        {configuration.content}
      </pre>
      <Button
        variant="ghost"
        size="sm"
        onClick={() => {
          const url = URL.createObjectURL(
            new Blob([configuration.content], { type: "text/plain" }),
          );
          const anchor = document.createElement("a");
          anchor.href = url;
          anchor.download = configuration.filename;
          anchor.click();
          URL.revokeObjectURL(url);
        }}
      >
        {t("models.local.saveConfiguration")}
      </Button>
    </details>
  );
}
