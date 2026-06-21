/**
 * API client for /api/documents/* — the RAG documents endpoints.
 *
 * Mirrors the backend app/api/documents.py routes:
 *   POST   /documents/upload            — multipart upload (sync digestion)
 *   POST   /documents/upload/stream     — multipart upload with SSE progress
 *   GET    /documents                   — list (optionally filter by scope/conversation)
 *   GET    /documents/{id}              — get one (with chunks)
 *   GET    /documents/{id}/download     — stream the raw file (returns a Blob)
 *   PATCH  /documents/{id}              — rename and/or toggle scope
 *   DELETE /documents/{id}              — delete (file + DB rows)
 *   GET    /conversations/{id}/documents — list docs attached to a conversation
 *
 * The /upload/stream endpoint is the one the Brain page uses — it emits
 * `document_digest_progress`, `document_digest_done`, `document_digest_error`
 * SSE events so the UI can show real-time extraction/chunking/embedding
 * progress.
 */

import { createDebugLogger, dbgError } from "@/lib/debug";

const log = createDebugLogger("documentsClient");

// ─── Types ────────────────────────────────────────────────────────────

export type DocumentScope = "private" | "public";
export type DigestionStatus = "pending" | "digesting" | "ready" | "failed";

export interface DocumentChunkDTO {
  id: string;
  chunk_index: number;
  text: string; // truncated to 500 chars by backend
  page_number: number | null;
  line_start: number | null;
  line_end: number | null;
  chunk_type: "text" | "image_description";
  has_image: boolean;
}

export interface DocumentDTO {
  id: string;
  filename: string;
  original_filename: string;
  mime_type: string;
  file_size_bytes: number;
  content_hash: string | null;
  scope: DocumentScope;
  conversation_id: string | null;
  message_id: string | null;
  total_pages: number | null;
  total_chunks: number;
  total_images: number;
  digestion_status: DigestionStatus;
  digestion_error: string | null;
  created_at: number; // ms epoch
  updated_at: number;
  chunks?: DocumentChunkDTO[];
}

export interface RetrievedSourceDTO {
  document_id: string;
  document_filename: string;
  chunk_id: string;
  text: string;
  snippet: string;
  page_number: number | null;
  line_start: number | null;
  line_end: number | null;
  chunk_type: "text" | "image_description";
  score: number;
  vector_sim: number;
  bm25_score: number;
  /** On-disk path of the original extracted image (relative to data dir).
   *  Only set for image_description chunks. The frontend fetches the
   *  actual bytes via /api/documents/chunks/{chunk_id}/image. */
  image_path?: string | null;
  has_image?: boolean;
}

// ─── Digestion progress (SSE event payload) ──────────────────────────

export interface DigestProgress {
  stage:
    | "started"
    | "extracting_text"
    | "extracting_images"
    | "chunking"
    | "describing_images"
    | "embedding"
    | "persisting"
    | "done"
    | "error";
  percent: number;
  details: string;
  filename?: string;
  document_id?: string | null;
  total_chunks?: number;
  total_images?: number;
  error?: string;
}

// ─── List / get ──────────────────────────────────────────────────────

export async function listDocuments(
  opts: { scope?: DocumentScope; conversationId?: string } = {},
): Promise<DocumentDTO[]> {
  const params = new URLSearchParams();
  if (opts.scope) params.set("scope", opts.scope);
  if (opts.conversationId) params.set("conversation_id", opts.conversationId);
  const qs = params.toString() ? `?${params.toString()}` : "";
  log(`➡️  listDocuments  qs=${qs || "(none)"}`);
  try {
    const res = await fetch(`/api/documents${qs}`);
    if (res.ok) {
      const data = await res.json();
      return data.documents ?? [];
    }
    dbgError(`   ❌ listDocuments status=${res.status}`);
  } catch (err) {
    dbgError(`   ❌ listDocuments error: ${err}`);
  }
  return [];
}

export async function getDocument(id: string): Promise<DocumentDTO | null> {
  log(`➡️  getDocument  id=${id}`);
  try {
    const res = await fetch(`/api/documents/${id}`);
    if (res.ok) return await res.json();
    dbgError(`   ❌ getDocument status=${res.status}`);
  } catch (err) {
    dbgError(`   ❌ getDocument error: ${err}`);
  }
  return null;
}

export async function listDocumentsForConversation(
  conversationId: string,
): Promise<DocumentDTO[]> {
  log(`➡️  listDocumentsForConversation  conv=${conversationId}`);
  try {
    const res = await fetch(`/api/conversations/${conversationId}/documents`);
    if (res.ok) {
      const data = await res.json();
      return data.documents ?? [];
    }
    dbgError(`   ❌ listDocumentsForConversation status=${res.status}`);
  } catch (err) {
    dbgError(`   ❌ listDocumentsForConversation error: ${err}`);
  }
  return [];
}

// ─── Download ────────────────────────────────────────────────────────

export async function downloadDocument(doc: DocumentDTO): Promise<void> {
  log(`➡️  downloadDocument  id=${doc.id}  filename=${doc.filename}`);
  try {
    const res = await fetch(`/api/documents/${doc.id}/download`);
    if (!res.ok) {
      dbgError(`   ❌ downloadDocument status=${res.status}`);
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = doc.filename || "download";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch (err) {
    dbgError(`   ❌ downloadDocument error: ${err}`);
  }
}

// ─── Update (rename / toggle scope) ──────────────────────────────────

export async function updateDocument(
  id: string,
  updates: {
    filename?: string;
    scope?: DocumentScope;
    conversation_id?: string | null;
  },
): Promise<DocumentDTO | null> {
  log(`➡️  updateDocument  id=${id}  updates=${JSON.stringify(updates)}`);
  try {
    const res = await fetch(`/api/documents/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(updates),
    });
    if (res.ok) return await res.json();
    const text = await res.text();
    dbgError(`   ❌ updateDocument status=${res.status}  body=${text}`);
  } catch (err) {
    dbgError(`   ❌ updateDocument error: ${err}`);
  }
  return null;
}

// ─── Delete ──────────────────────────────────────────────────────────

export async function deleteDocument(id: string): Promise<boolean> {
  log(`➡️  deleteDocument  id=${id}`);
  try {
    const res = await fetch(`/api/documents/${id}`, { method: "DELETE" });
    if (res.ok) return true;
    dbgError(`   ❌ deleteDocument status=${res.status}`);
  } catch (err) {
    dbgError(`   ❌ deleteDocument error: ${err}`);
  }
  return false;
}

// ─── Upload (SSE streaming with progress) ────────────────────────────

export interface UploadOptions {
  scope: DocumentScope;
  conversationId?: string; // required when scope === "private"
  onProgress?: (p: DigestProgress) => void;
  onDone?: (doc: DocumentDTO) => void;
  onError?: (error: string) => void;
}

/**
 * Upload a document with real-time SSE digestion progress.
 *
 * The backend emits `document_digest_progress`, `document_digest_done`,
 * and `document_digest_error` events; we parse them and invoke the
 * matching callback. Resolves when digestion completes (done or error).
 */
export async function uploadDocumentStream(
  file: File,
  opts: UploadOptions,
): Promise<void> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("scope", opts.scope);
  if (opts.scope === "private") {
    if (!opts.conversationId) {
      throw new Error("conversationId is required for private uploads");
    }
    formData.append("conversation_id", opts.conversationId);
  }

  log(
    `➡️  uploadDocumentStream  filename=${file.name}  size=${file.size}  scope=${opts.scope}  conv=${opts.conversationId ?? "(none)"}`,
  );

  let res: Response;
  try {
    res = await fetch("/api/documents/upload/stream", {
      method: "POST",
      body: formData,
    });
  } catch (err) {
    dbgError(`   ❌ uploadDocumentStream fetch error: ${err}`);
    opts.onError?.(err instanceof Error ? err.message : String(err));
    return;
  }

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    dbgError(`   ❌ uploadDocumentStream status=${res.status}  body=${text}`);
    opts.onError?.(`Upload failed (HTTP ${res.status}): ${text}`);
    return;
  }

  // Parse SSE manually — same pattern as stream.ts but standalone.
  const reader = res.body?.getReader();
  if (!reader) {
    opts.onError?.("No response body for SSE stream");
    return;
  }

  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const data = line.slice(6).trim();
        if (!data) continue;
        try {
          const parsed = JSON.parse(data);
          const event = parsed.event;
          if (event === "document_digest_progress") {
            opts.onProgress?.({
              stage: parsed.stage,
              percent: parsed.percent,
              details: parsed.details,
              filename: parsed.filename,
              document_id: parsed.document_id,
              total_chunks: parsed.total_chunks,
              total_images: parsed.total_images,
            });
          } else if (event === "document_digest_done") {
            const doc: DocumentDTO = parsed.document;
            opts.onProgress?.({
              stage: "done",
              percent: 100,
              details: `Digested ${doc.filename}: ${doc.total_chunks} chunks`,
              filename: doc.filename,
              document_id: doc.id,
              total_chunks: doc.total_chunks,
              total_images: doc.total_images,
            });
            opts.onDone?.(doc);
            return;
          } else if (event === "document_digest_error") {
            const errMsg = parsed.error || "Digestion failed";
            opts.onProgress?.({
              stage: "error",
              percent: 100,
              details: errMsg,
              filename: parsed.filename,
              document_id: parsed.document_id,
              error: errMsg,
            });
            opts.onError?.(errMsg);
            return;
          }
        } catch {
          /* skip malformed JSON */
        }
      }
    }
    // Stream ended without explicit done/error
    opts.onError?.("Upload stream ended unexpectedly");
  } catch (err) {
    dbgError(`   ❌ uploadDocumentStream read error: ${err}`);
    opts.onError?.(err instanceof Error ? err.message : String(err));
  }
}
