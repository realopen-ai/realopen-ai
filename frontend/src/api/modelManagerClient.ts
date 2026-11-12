/** Model catalogs and inference runtimes are separate server-discovered registries. */
export interface ModelImportConfiguration {
  kind: string;
  filename: string;
  content: string;
}
export interface ModelVariant {
  id: string;
  label: string;
  format: string;
  size: number | null;
  quantization: string | null;
  files: { path: string; size: number }[];
  compatibility: Record<string, { supported: boolean; reason: string }>;
  configurations?: Record<
    string,
    { kind: string; filename: string; content: string }
  >;
}
export interface CatalogModel {
  catalog: string;
  id: string;
  title: string;
  url: string;
  description: string;
  revision?: string | null;
  license?: string | null;
  gated?: boolean;
  variants: ModelVariant[];
  requested_variant?: string | null;
}
export interface InstalledModel {
  name: string;
  runtime: string;
  size: number | null;
  quantization: string | null;
  family: string | null;
}
export interface LocalImportOptions {
  extensions: string[];
  companion_files: string[];
  chunk_size: number;
  max_bytes: number;
}
export interface ModelInventory {
  local_import?: LocalImportOptions | null;
  models: InstalledModel[];
  runtimes: {
    id: string;
    label: string;
    available: boolean;
    version: string | null;
  }[];
  catalogs: { id: string; label: string }[];
}
export interface ModelInstallRequest {
  catalog: string;
  model: string;
  variant: string;
  runtime: string;
  name: string;
  revision?: string | null;
}
export interface ModelJob {
  id: string;
  request: ModelInstallRequest;
  status: string;
  stage: string;
  message: string;
  completed: number;
  total: number;
  source_url: string;
  source_title?: string;
  configuration?: ModelImportConfiguration;
  created_at: string;
  updated_at: string;
  capabilities?: string[];
  files?: { path: string; size: number }[];
}

const base = "/api/model-manager";
async function request<T>(
  path: string,
  init?: RequestInit,
  rateLimitRetries = 0,
): Promise<T> {
  const response = await fetch(base + path, init);
  if (response.status === 429 && rateLimitRetries > 0) {
    // Sequential model chunks may briefly exceed the existing proxy API rate
    // limit on a fast local connection. Back off instead of losing the upload.
    const delay = Math.min(
      5000,
      Math.max(
        500,
        Number(response.headers.get("Retry-After") || "1") * 1000 || 1000,
      ),
    );
    await response.body?.cancel();
    await new Promise<void>((resolve, reject) => {
      const cancel = () => {
        clearTimeout(timer);
        reject(new DOMException("Upload cancelled", "AbortError"));
      };
      const timer = setTimeout(() => {
        init?.signal?.removeEventListener("abort", cancel);
        resolve();
      }, delay);
      if (init?.signal?.aborted) cancel();
      else init?.signal?.addEventListener("abort", cancel, { once: true });
    });
    return request<T>(path, init, rateLimitRetries - 1);
  }
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(
      typeof error?.detail === "string"
        ? error.detail
        : `Model manager request failed (${response.status})`,
    );
  }
  return response.json() as Promise<T>;
}
const body = (value: unknown) => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(value),
});
export const modelManager = {
  inventory: () => request<ModelInventory>(""),
  catalogs: () =>
    request<{ catalogs: ModelInventory["catalogs"] }>("/catalogs"),
  jobs: () => request<{ jobs: ModelJob[] }>("/jobs"),
  search: (catalog: string, q: string, signal?: AbortSignal) =>
    request<{ models: CatalogModel[] }>(
      `/search?${new URLSearchParams({ catalog, q })}`,
      { signal },
    ),
  inspect: (catalog: string, reference: string, signal?: AbortSignal) =>
    request<CatalogModel>("/inspect", {
      ...body({ catalog, reference }),
      signal,
    }),
  install: (value: ModelInstallRequest) =>
    request<ModelJob>("/jobs", body(value)),
  cancel: (id: string) =>
    request<ModelJob>(`/jobs/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
    }),
  retry: (id: string) =>
    request<ModelJob>(`/jobs/${encodeURIComponent(id)}/retry`, {
      method: "POST",
    }),
  remove: (runtime: string, name: string) =>
    request<{ removed: boolean }>("/remove", body({ runtime, name })),
  inspectLocalPath: (path: string, format_hint: string, signal?: AbortSignal) =>
    request<CatalogModel>("/local/path", {
      ...body({ path, format_hint }),
      signal,
    }),
  beginLocalUpload: (
    title: string,
    files: { path: string; size: number }[],
    format_hint: string,
    signal?: AbortSignal,
  ) =>
    request<{ id: string; chunk_size: number }>("/local/uploads", {
      ...body({ title, files, format_hint }),
      signal,
    }),
  uploadLocalChunk: (
    id: string,
    path: string,
    offset: number,
    data: Blob,
    signal?: AbortSignal,
  ) =>
    request<{ offset: number }>(
      `/local/uploads/${encodeURIComponent(id)}/file?${new URLSearchParams({ path, offset: String(offset) })}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/octet-stream" },
        body: data,
        signal,
      },
      4,
    ),
  finishLocalUpload: (id: string, signal?: AbortSignal) =>
    request<CatalogModel>(`/local/uploads/${encodeURIComponent(id)}/finish`, {
      method: "POST",
      signal,
    }),
  discardLocalUpload: (id: string) =>
    request<{ discarded: boolean }>(
      `/local/uploads/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    ),
  events: () => new EventSource(base + "/events"),
};
