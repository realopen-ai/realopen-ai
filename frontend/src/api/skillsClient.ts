export type SkillRole = "general" | "coder" | "voice";

export interface SkillSummary {
  id: string;
  name: string;
  description: string;
  roles: SkillRole[];
  enabled: boolean;
  files: string[];
  content?: string;
}

export interface SkillInput {
  name: string;
  description: string;
  roles: SkillRole[];
  enabled: boolean;
  content: string;
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body?.detail || `Request failed (${response.status})`);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

export const skillsClient = {
  list: () => request<SkillSummary[]>("/api/skills"),
  get: (id: string) =>
    request<SkillSummary>(`/api/skills/${encodeURIComponent(id)}`),
  resource: async (id: string, path: string) => {
    const resourcePath = path.split("/").map(encodeURIComponent).join("/");
    const response = await fetch(
      `/api/skills/${encodeURIComponent(id)}/resources/${resourcePath}`,
    );
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      throw new Error(body?.detail || `Request failed (${response.status})`);
    }
    return response.text();
  },
  create: (input: SkillInput) =>
    request<SkillSummary>("/api/skills", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  update: (id: string, input: SkillInput) =>
    request<SkillSummary>(`/api/skills/${encodeURIComponent(id)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  remove: (id: string) =>
    request<void>(`/api/skills/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  import: (form: FormData) =>
    request<SkillSummary>("/api/skills/import", { method: "POST", body: form }),
};
