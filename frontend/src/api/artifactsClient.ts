export interface ArtifactReference {
  artifact_id: string;
  version: number;
  section_id: string;
  page?: number;
  slide?: number;
  sheet?: string;
}
export interface Artifact {
  id: string;
  title: string;
  kind: string;
  version: number;
  editable: boolean;
  readable: boolean;
  export_formats: string[];
  document_id: string | null;
  conversation_id: string | null;
  created_at: string | null;
}
export interface ArtifactSection {
  id: string;
  title: string;
  characters: number;
  page?: number;
  slide?: number;
  sheet?: string;
  method?: string;
}
export interface ArtifactDetail extends Artifact {
  selected_version: number;
  outline: { sections: ArtifactSection[]; warnings: string[] };
  outputs: string[];
  previews: Record<string, string>;
  versions: {
    number: number;
    created_at: string;
    restored_from: number | null;
  }[];
}
export interface ArtifactRead {
  content: string;
  offset: number;
  next_offset: number | null;
  version: number;
  id: string;
}
async function request<T>(
  path: string,
  method = "GET",
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(`/api/artifacts${path}`, {
    method,
    signal,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const result = await response.json().catch(() => ({}));
    throw new Error(
      typeof result.detail === "string"
        ? result.detail
        : `Request failed (${response.status})`,
    );
  }
  return response.status === 204 ? (undefined as T) : response.json();
}
export const artifactsApi = {
  list: (offset = 0, signal?: AbortSignal) =>
    request<{ items: Artifact[]; next_offset: number | null }>(
      `?offset=${offset}`,
      "GET",
      undefined,
      signal,
    ),
  detail: (id: string, version?: number, signal?: AbortSignal) =>
    request<ArtifactDetail>(
      `/${encodeURIComponent(id)}${version ? `?version=${version}` : ""}`,
      "GET",
      undefined,
      signal,
    ),
  read: (
    id: string,
    version: number,
    section: string,
    offset = 0,
    signal?: AbortSignal,
  ) =>
    request<ArtifactRead>(
      `/${encodeURIComponent(id)}/read?version=${version}&section_id=${encodeURIComponent(section)}&offset=${offset}`,
      "GET",
      undefined,
      signal,
    ),
  update: (id: string, version: number, section: string, content: string) =>
    request<Artifact>(`/${encodeURIComponent(id)}`, "PUT", {
      expected_version: version,
      changes: [{ section_id: section, content }],
    }),
  restore: (id: string, expected: number, version: number) =>
    request<Artifact>(`/${encodeURIComponent(id)}/restore`, "POST", {
      expected_version: expected,
      version,
    }),
  export: (id: string, version: number, format: string) =>
    request<{ download_url: string }>(
      `/${encodeURIComponent(id)}/export/${encodeURIComponent(format)}?version=${version}`,
      "POST",
    ),
};
export function artifactSourceUrl(source: ArtifactReference) {
  return `/workspace/artifacts/${encodeURIComponent(source.artifact_id)}?version=${source.version}&section=${encodeURIComponent(source.section_id)}`;
}
