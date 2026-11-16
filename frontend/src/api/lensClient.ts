export interface LensImage {
  id: string;
  filename: string;
  conversation_id: string | null;
  document_id: string | null;
  created_at: string;
  ocr_text?: string | null;
}

interface LensState {
  total: number;
  ocr: number;
  visual: number;
  with_text: number;
  semantic: number;
  failed: number;
  siglip2_installed: boolean;
  e5_installed: boolean;
}

interface LensResults {
  items: LensImage[];
  more?: boolean;
  state: LensState;
}

const imagesUrl = "/api/lens/images";
const imageUrl = (id: string) => `${imagesUrl}/${encodeURIComponent(id)}`;

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(typeof error?.detail === "string" ? error.detail : "");
  }
  return response.json() as Promise<T>;
}

export const lensClient = {
  list: (q: string, offset: number, signal?: AbortSignal) =>
    request<LensResults>(
      `${imagesUrl}?${new URLSearchParams({ q, offset: String(offset) })}`,
      { signal },
    ),
  similar: (file: File, signal?: AbortSignal) => {
    const body = new FormData();
    body.append("file", file);
    return request<LensResults>("/api/lens/similar", {
      method: "POST",
      body,
      signal,
    });
  },
  detail: (id: string, signal?: AbortSignal) =>
    request<LensImage>(imageUrl(id), { signal }),
  thumbnail: (id: string) => `${imageUrl(id)}/thumbnail`,
  preview: (id: string) => `${imageUrl(id)}/preview`,
};
