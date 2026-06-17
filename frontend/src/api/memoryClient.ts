import { dbgError, createDebugLogger } from "@/lib/debug";

const log = createDebugLogger("memoryClient");

// ─── Types ──────────────────────────────────────────────────────

export interface MemoryItem {
  id: string;
  text: string;
  category: string; // identity, preference, fact, contact, project, goal
  source: string; // auto, user, ai_agent
  pinned: boolean;
  uses: number;
  conversation_id: string | null;
  created_at: number;
  updated_at: number;
}

export interface CategoryCount {
  category: string;
  count: number;
}

// ─── API Functions ──────────────────────────────────────────────

export async function fetchMemories(category?: string): Promise<MemoryItem[]> {
  const url = category
    ? `/api/memory?category=${encodeURIComponent(category)}`
    : "/api/memory";
  log(`➡️  fetchMemories  url=${url}`);
  try {
    const res = await fetch(url);
    log(`   response  status=${res.status}  ok=${res.ok}`);
    if (res.ok) {
      const data = await res.json();
      return data.memories ?? data ?? [];
    }
    dbgError(`   ❌ fetchMemories NOT OK  status=${res.status}`);
  } catch (err) {
    dbgError(`   ❌ fetchMemories error: ${err}`);
  }
  return [];
}

export async function fetchMemoryCategories(): Promise<CategoryCount[]> {
  log("➡️  fetchMemoryCategories  url=/api/memory/categories");
  try {
    const res = await fetch("/api/memory/categories");
    log(`   response  status=${res.status}  ok=${res.ok}`);
    if (res.ok) {
      const data = await res.json();
      return data.categories ?? data ?? [];
    }
    dbgError(`   ❌ fetchMemoryCategories NOT OK  status=${res.status}`);
  } catch (err) {
    dbgError(`   ❌ fetchMemoryCategories error: ${err}`);
  }
  return [];
}

export async function addMemory(
  text: string,
  category: string,
  source?: string,
): Promise<MemoryItem | null> {
  log(`➡️  addMemory  category=${category}`);
  try {
    const body: Record<string, string> = { text, category };
    if (source) body.source = source;
    const res = await fetch("/api/memory", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    log(`   response  status=${res.status}  ok=${res.ok}`);
    if (res.ok) return await res.json();
    dbgError(`   ❌ addMemory NOT OK  status=${res.status}`);
  } catch (err) {
    dbgError(`   ❌ addMemory error: ${err}`);
  }
  return null;
}

export async function updateMemory(
  id: string,
  text: string,
  category?: string,
): Promise<boolean> {
  log(`➡️  updateMemory  id=${id}`);
  try {
    const body: Record<string, string> = { text };
    if (category) body.category = category;
    const res = await fetch(`/api/memory/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    log(`   response  status=${res.status}  ok=${res.ok}`);
    return res.ok;
  } catch (err) {
    dbgError(`   ❌ updateMemory error: ${err}`);
  }
  return false;
}

export async function deleteMemory(id: string): Promise<boolean> {
  log(`➡️  deleteMemory  id=${id}`);
  try {
    const res = await fetch(`/api/memory/${id}`, {
      method: "DELETE",
    });
    log(`   response  status=${res.status}  ok=${res.ok}`);
    return res.ok;
  } catch (err) {
    dbgError(`   ❌ deleteMemory error: ${err}`);
  }
  return false;
}

export async function pinMemory(id: string, pinned: boolean): Promise<boolean> {
  log(`➡️  pinMemory  id=${id}  pinned=${pinned}`);
  try {
    const res = await fetch(`/api/memory/${id}/pin`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pinned }),
    });
    log(`   response  status=${res.status}  ok=${res.ok}`);
    return res.ok;
  } catch (err) {
    dbgError(`   ❌ pinMemory error: ${err}`);
  }
  return false;
}

export async function searchMemories(
  query: string,
  category?: string,
): Promise<MemoryItem[]> {
  log(`➡️  searchMemories  query=${query}`);
  try {
    const body: Record<string, string> = { query };
    if (category) body.category = category;
    const res = await fetch("/api/memory/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    log(`   response  status=${res.status}  ok=${res.ok}`);
    if (res.ok) {
      const data = await res.json();
      return data.memories ?? data ?? [];
    }
    dbgError(`   ❌ searchMemories NOT OK  status=${res.status}`);
  } catch (err) {
    dbgError(`   ❌ searchMemories error: ${err}`);
  }
  return [];
}

export async function auditMemories(): Promise<{
  before: number;
  after: number;
  removed: number;
} | null> {
  log("➡️  auditMemories");
  try {
    const res = await fetch("/api/memory/audit", {
      method: "POST",
    });
    log(`   response  status=${res.status}  ok=${res.ok}`);
    if (res.ok) return await res.json();
    dbgError(`   ❌ auditMemories NOT OK  status=${res.status}`);
  } catch (err) {
    dbgError(`   ❌ auditMemories error: ${err}`);
  }
  return null;
}
