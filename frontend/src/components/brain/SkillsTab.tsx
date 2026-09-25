import { useEffect, useMemo, useRef, useState } from "react";
import type { InputHTMLAttributes, ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { markdownCodeComponents } from "@/components/chat/MarkdownCodeBlock";
import { HighlightedCode } from "@/components/ui/HighlightedCode";
import {
  ArchiveRestore,
  ChevronLeft,
  Boxes,
  Code2,
  FileText,
  Folder,
  FolderUp,
  Globe2,
  Mic2,
  Pencil,
  Plus,
  Search,
  Trash2,
  X,
} from "lucide-react";
import {
  skillsClient,
  type SkillInput,
  type SkillRole,
  type SkillSummary,
} from "@/api/skillsClient";

const ROLES: SkillRole[] = ["general", "coder", "voice"];
const EMPTY: SkillInput = {
  name: "",
  description: "",
  roles: [],
  enabled: true,
  content:
    "# Instructions\n\nDescribe when and how the agent should use this skill.\n",
};

export function SkillsTab() {
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [query, setQuery] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<SkillInput | null>(null);
  const [selectedSkill, setSelectedSkill] = useState<SkillSummary | null>(null);
  const [selectedResource, setSelectedResource] = useState<string | null>(null);
  const [resourceContent, setResourceContent] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const archiveRef = useRef<HTMLInputElement>(null);
  const folderRef = useRef<HTMLInputElement>(null);

  const refresh = async () => {
    try {
      setSkills(await skillsClient.list());
      setError(null);
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "Failed to load skills",
      );
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return needle
      ? skills.filter((skill) =>
          `${skill.name} ${skill.description}`.toLowerCase().includes(needle),
        )
      : skills;
  }, [query, skills]);

  const edit = async (id: string) => {
    setBusy(true);
    try {
      const skill = await skillsClient.get(id);
      setEditingId(id);
      setDraft({
        name: skill.name,
        description: skill.description,
        roles: skill.roles,
        enabled: skill.enabled,
        content: skill.content || "",
      });
      setSelectedSkill(null);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Failed to open skill");
    } finally {
      setBusy(false);
    }
  };

  const view = async (id: string) => {
    setBusy(true);
    try {
      setSelectedSkill(await skillsClient.get(id));
      setSelectedResource(null);
      setResourceContent(null);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Failed to open skill");
    } finally {
      setBusy(false);
    }
  };

  const viewResource = async (path: string) => {
    if (!selectedSkill) return;
    setSelectedResource(path);
    setResourceContent(null);
    try {
      setResourceContent(await skillsClient.resource(selectedSkill.id, path));
      setError(null);
    } catch (cause) {
      setResourceContent(
        cause instanceof Error
          ? cause.message
          : "This resource cannot be previewed",
      );
    }
  };

  const save = async () => {
    if (!draft) return;
    setBusy(true);
    try {
      if (editingId) await skillsClient.update(editingId, draft);
      else await skillsClient.create(draft);
      setDraft(null);
      setEditingId(null);
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Failed to save skill");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (skill: SkillSummary) => {
    setBusy(true);
    try {
      await skillsClient.remove(skill.id);
      setDeletingId(null);
      await refresh();
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "Failed to delete skill",
      );
    } finally {
      setBusy(false);
    }
  };

  const importFiles = async (files: FileList | null, archive: boolean) => {
    if (!files?.length) return;
    setBusy(true);
    try {
      const form = new FormData();
      for (const file of Array.from(files)) {
        form.append(
          archive ? "archive" : "files",
          file,
          file.webkitRelativePath || file.name,
        );
      }
      await skillsClient.import(form);
      await refresh();
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "Failed to import skill",
      );
    } finally {
      setBusy(false);
      if (archiveRef.current) archiveRef.current.value = "";
      if (folderRef.current) folderRef.current.value = "";
    }
  };

  if (selectedSkill) {
    const resources = selectedSkill.files.filter((path) => path !== "SKILL.md");
    return (
      <div className="h-full overflow-y-auto">
        <div className="mx-auto flex w-full max-w-6xl flex-col gap-7 px-8 py-8 lg:px-10">
          <div className="flex items-start justify-between gap-6 border-b border-border/60 pb-6">
            <div className="flex min-w-0 items-start gap-4">
              <button
                aria-label="Back to skills"
                className="mt-1 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                onClick={() => setSelectedSkill(null)}
              >
                <ChevronLeft className="h-4 w-4" />
              </button>
              <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-xl border border-primary/20 bg-primary/10 text-primary">
                <Code2 className="h-5 w-5" />
              </div>
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2.5">
                  <h2 className="text-xl font-semibold tracking-tight">
                    {selectedSkill.name}
                  </h2>
                  <span
                    className={`rounded-md px-2 py-1 text-[11px] font-medium ${selectedSkill.enabled ? "bg-emerald-500/10 text-emerald-500" : "bg-muted text-muted-foreground"}`}
                  >
                    {selectedSkill.enabled ? "Enabled" : "Disabled"}
                  </span>
                </div>
                <p className="mt-1.5 max-w-2xl text-sm leading-5 text-muted-foreground">
                  {selectedSkill.description}
                </p>
              </div>
            </div>
            <button
              className="flex h-10 shrink-0 items-center gap-2 rounded-lg border border-border px-4 text-sm font-medium transition-colors hover:bg-muted"
              onClick={() => void edit(selectedSkill.id)}
            >
              <Pencil className="h-4 w-4" /> Edit skill
            </button>
          </div>

          <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_300px]">
            <main className="min-w-0">
              <div className="mb-3 flex items-center justify-between">
                <h3 className="text-sm font-semibold">Instructions</h3>
                <span className="text-xs text-muted-foreground">SKILL.md</span>
              </div>
              <div className="min-h-96 rounded-2xl border border-border/70 bg-card/40 p-6">
                <div className="prose prose-sm dark:prose-invert max-w-none leading-relaxed">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={markdownCodeComponents}
                  >
                    {selectedSkill.content || "_No instructions provided._"}
                  </ReactMarkdown>
                </div>
              </div>
            </main>

            <aside className="flex min-w-0 flex-col gap-6">
              <section>
                <h3 className="mb-3 text-sm font-semibold">Available to</h3>
                <div className="flex flex-wrap gap-2">
                  {(selectedSkill.roles.length
                    ? selectedSkill.roles
                    : ["global"]
                  ).map((role) => (
                    <span
                      key={role}
                      className="flex items-center gap-1.5 rounded-lg border border-border bg-card/50 px-3 py-2 text-xs font-medium capitalize text-muted-foreground"
                    >
                      {role === "general" || role === "global" ? (
                        <Globe2 className="h-3.5 w-3.5" />
                      ) : role === "coder" ? (
                        <Code2 className="h-3.5 w-3.5" />
                      ) : (
                        <Mic2 className="h-3.5 w-3.5" />
                      )}
                      {role}
                    </span>
                  ))}
                </div>
              </section>

              <section>
                <div className="mb-3 flex items-center justify-between">
                  <h3 className="text-sm font-semibold">Resources</h3>
                  <span className="text-xs text-muted-foreground">
                    {resources.length}
                  </span>
                </div>
                <div className="overflow-hidden rounded-xl border border-border/70 bg-card/40">
                  {resources.length ? (
                    resources.map((path) => (
                      <button
                        key={path}
                        className={`flex w-full items-center gap-2.5 border-b border-border/60 px-3 py-3 text-left text-xs last:border-b-0 cursor-pointer ${selectedResource === path ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-muted/60 hover:text-foreground"}`}
                        onClick={() => void viewResource(path)}
                      >
                        <FileText className="h-4 w-4 shrink-0" />
                        <span className="min-w-0 truncate">{path}</span>
                      </button>
                    ))
                  ) : (
                    <div className="flex flex-col items-center px-4 py-8 text-center text-muted-foreground">
                      <Folder className="mb-2 h-5 w-5 opacity-50" />
                      <span className="text-xs">No bundled resources</span>
                    </div>
                  )}
                </div>
              </section>
            </aside>
          </div>

          {selectedResource && (
            <section>
              <div className="mb-3 flex items-center justify-between">
                <h3 className="truncate text-sm font-semibold">
                  {selectedResource}
                </h3>
                <button
                  className="text-xs text-muted-foreground hover:text-foreground"
                  onClick={() => {
                    setSelectedResource(null);
                    setResourceContent(null);
                  }}
                >
                  Close preview
                </button>
              </div>
              <pre className="max-h-128 overflow-auto rounded-2xl border border-border/70 bg-sandbox-bg p-5 font-mono text-xs leading-5">
                <HighlightedCode
                  code={resourceContent ?? "Loading resource…"}
                  filePath={selectedResource}
                />
              </pre>
            </section>
          )}
        </div>
      </div>
    );
  }

  if (draft) {
    return (
      <div className="h-full overflow-y-auto">
        <div className="mx-auto flex w-full max-w-5xl flex-col gap-7 px-8 py-8 lg:px-10">
          <div className="flex items-start justify-between gap-6 border-b border-border/60 pb-6">
            <div className="flex items-start gap-4">
              <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border border-primary/20 bg-primary/10 text-primary">
                <FileText className="h-5 w-5" />
              </div>
              <div>
                <h2 className="text-xl font-semibold tracking-tight">
                  {editingId ? "Edit skill" : "Create skill"}
                </h2>
                <p className="mt-1 text-sm text-muted-foreground">
                  Saved as a SKILL.md file with YAML metadata.
                </p>
              </div>
            </div>
            <button
              aria-label="Close editor"
              className="flex h-9 w-9 items-center justify-center rounded-lg border border-border text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
              onClick={() => setDraft(null)}
            >
              <X className="h-4.5 w-4.5" />
            </button>
          </div>

          <section className="grid gap-5 rounded-xl border border-border/70 bg-card/40 p-5 sm:grid-cols-2">
            <Field label="Name">
              <input
                className="h-10 w-full rounded-lg border border-border bg-background px-3 text-sm outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-primary/10"
                value={draft.name}
                onChange={(event) =>
                  setDraft({ ...draft, name: event.target.value })
                }
                placeholder="Code review"
              />
            </Field>
            <Field label="Description">
              <input
                className="h-10 w-full rounded-lg border border-border bg-background px-3 text-sm outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-primary/10"
                value={draft.description}
                onChange={(event) =>
                  setDraft({ ...draft, description: event.target.value })
                }
                placeholder="Review changes for correctness and regressions"
              />
            </Field>
          </section>

          <section className="rounded-xl border border-border/70 bg-card/40 p-5">
            <Field label="Available to">
              <div className="mt-1 flex flex-wrap items-center gap-2.5">
                {ROLES.map((role) => (
                  <button
                    key={role}
                    type="button"
                    className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-xs font-medium capitalize transition-colors ${
                      draft.roles.includes(role)
                        ? "border-primary/40 bg-primary/10 text-primary"
                        : "border-border text-muted-foreground hover:bg-muted"
                    }`}
                    onClick={() =>
                      setDraft({
                        ...draft,
                        roles: draft.roles.includes(role)
                          ? draft.roles.filter((item) => item !== role)
                          : [...draft.roles, role],
                      })
                    }
                  >
                    {role === "general" ? (
                      <Globe2 className="h-3.5 w-3.5" />
                    ) : role === "coder" ? (
                      <Code2 className="h-3.5 w-3.5" />
                    ) : (
                      <Mic2 className="h-3.5 w-3.5" />
                    )}
                    {role}
                  </button>
                ))}
                <span className="self-center text-xs text-muted-foreground">
                  {draft.roles.length
                    ? "Selected roles only"
                    : "Global (all roles)"}
                </span>
              </div>
            </Field>
          </section>

          <Field label="Instructions (Markdown)">
            <textarea
              className="min-h-96 w-full resize-y rounded-xl border border-border bg-card/40 p-5 font-mono text-[13px] leading-6 outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-primary/10"
              value={draft.content}
              onChange={(event) =>
                setDraft({ ...draft, content: event.target.value })
              }
            />
          </Field>

          <label className="flex items-center gap-3 rounded-xl border border-border/70 bg-card/40 p-4 text-sm">
            <input
              type="checkbox"
              checked={draft.enabled}
              onChange={(event) =>
                setDraft({ ...draft, enabled: event.target.checked })
              }
            />
            Enabled for agent routing
          </label>

          {error && <p className="text-sm text-destructive">{error}</p>}
          <div className="sticky bottom-0 flex justify-end gap-3 border-t border-border/60 bg-background/95 py-4 backdrop-blur">
            <button
              className="h-10 rounded-lg border border-border px-5 text-sm font-medium hover:bg-muted"
              onClick={() => setDraft(null)}
            >
              Cancel
            </button>
            <button
              className="h-10 rounded-lg bg-primary px-5 text-sm font-medium text-primary-foreground shadow-sm disabled:opacity-50"
              disabled={busy || !draft.name.trim() || !draft.description.trim()}
              onClick={() => void save()}
            >
              {busy ? "Saving…" : "Save skill"}
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex w-full max-w-7xl flex-col gap-7 px-8 py-8 lg:px-10 lg:py-10">
        <div className="flex flex-wrap items-start justify-between gap-6">
          <div className="flex min-w-0 items-start gap-4">
            <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-xl border border-primary/20 bg-primary/10 text-primary">
              <Boxes className="h-5.5 w-5.5" />
            </div>
            <div>
              <h2 className="text-xl font-semibold tracking-tight">Skills</h2>
              <p className="mt-1 max-w-2xl text-sm leading-5 text-muted-foreground">
                Reusable instructions and resources. Agents see only names and
                descriptions until they load a relevant skill.
              </p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2.5">
            <button
              className="flex h-10 items-center gap-2 rounded-lg border border-border bg-background px-4 text-sm font-medium transition-colors hover:bg-muted disabled:opacity-50"
              disabled={busy}
              onClick={() => archiveRef.current?.click()}
            >
              <ArchiveRestore className="h-4 w-4 shrink-0" />{" "}
              <span>Import ZIP</span>
            </button>
            <button
              className="flex h-10 items-center gap-2 rounded-lg border border-border bg-background px-4 text-sm font-medium transition-colors hover:bg-muted disabled:opacity-50"
              disabled={busy}
              onClick={() => folderRef.current?.click()}
            >
              <FolderUp className="h-4 w-4 shrink-0" />{" "}
              <span>Import folder</span>
            </button>
            <button
              className="flex h-10 items-center gap-2 rounded-lg bg-primary px-4 text-sm font-medium text-primary-foreground shadow-sm transition-opacity hover:opacity-90"
              onClick={() => setDraft({ ...EMPTY })}
            >
              <Plus className="h-4 w-4 shrink-0" /> <span>New skill</span>
            </button>
            <input
              ref={archiveRef}
              hidden
              type="file"
              accept=".zip,application/zip"
              onChange={(e) => void importFiles(e.target.files, true)}
            />
            <input
              ref={folderRef}
              hidden
              type="file"
              multiple
              {...({
                webkitdirectory: "",
                directory: "",
              } as InputHTMLAttributes<HTMLInputElement>)}
              onChange={(e) => void importFiles(e.target.files, false)}
            />
          </div>
        </div>

        <div className="flex items-center gap-4 border-b border-border/60 pb-6">
          <div className="relative w-full max-w-xl">
            <Search className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <input
              className="h-11 w-full rounded-xl border border-border bg-card/40 pl-10 pr-4 text-sm outline-none transition-colors placeholder:text-muted-foreground/70 focus:border-primary focus:ring-2 focus:ring-primary/10"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search skills…"
            />
          </div>
          <span className="shrink-0 text-xs text-muted-foreground">
            {filtered.length} {filtered.length === 1 ? "skill" : "skills"}
          </span>
        </div>
        {error && <p className="text-sm text-destructive">{error}</p>}

        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {filtered.map((skill) => (
            <article
              key={skill.id}
              role="button"
              tabIndex={0}
              className="group flex min-h-56 cursor-pointer flex-col rounded-2xl border border-border/80 bg-card/50 p-5 transition-colors hover:border-primary/30 hover:bg-card focus:outline-none focus:ring-2 focus:ring-primary/30"
              onClick={() => void view(skill.id)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ")
                  void view(skill.id);
              }}
            >
              <div className="flex min-w-0 items-start gap-3.5">
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-border bg-background text-primary shadow-sm">
                  <Code2 className="h-4.5 w-4.5" />
                </div>
                <div className="min-w-0 flex-1 pt-0.5">
                  <div className="flex items-center gap-2">
                    <h3 className="truncate text-[15px] font-semibold">
                      {skill.name}
                    </h3>
                    {!skill.enabled && (
                      <span className="rounded-md bg-muted px-2 py-0.5 text-[10px] font-medium text-muted-foreground">
                        Disabled
                      </span>
                    )}
                  </div>
                  <p className="mt-1.5 line-clamp-3 text-[13px] leading-5 text-muted-foreground">
                    {skill.description}
                  </p>
                </div>
              </div>
              <div className="mt-auto pt-6">
                <div className="flex items-center justify-between gap-3 text-[11px] text-muted-foreground">
                  <div className="flex flex-wrap gap-1.5">
                    {(skill.roles.length ? skill.roles : ["global"]).map(
                      (role) => (
                        <span
                          key={role}
                          className="rounded-md border border-border bg-background px-2 py-1 font-medium capitalize"
                        >
                          {role}
                        </span>
                      ),
                    )}
                  </div>
                  <span className="flex shrink-0 items-center gap-1.5">
                    <FileText className="h-3.5 w-3.5" />
                    {Math.max(skill.files.length - 1, 0)} resources
                  </span>
                </div>
                <div className="mt-4 flex min-h-9 items-center justify-end gap-2 border-t border-border/60 pt-4">
                  {deletingId === skill.id ? (
                    <>
                      <span className="mr-auto text-xs text-muted-foreground">
                        Delete this skill?
                      </span>
                      <button
                        className="h-8 rounded-lg border border-border px-3 text-xs font-medium hover:bg-muted"
                        disabled={busy}
                        onClick={(event) => {
                          event.stopPropagation();
                          setDeletingId(null);
                        }}
                      >
                        Cancel
                      </button>
                      <button
                        className="h-8 rounded-lg bg-destructive px-3 text-xs font-medium text-destructive-foreground disabled:opacity-50"
                        disabled={busy}
                        onClick={(event) => {
                          event.stopPropagation();
                          void remove(skill);
                        }}
                      >
                        Delete
                      </button>
                    </>
                  ) : (
                    <>
                      <button
                        className="flex h-8 items-center gap-1.5 rounded-lg px-3 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                        disabled={busy}
                        onClick={(event) => {
                          event.stopPropagation();
                          void edit(skill.id);
                        }}
                      >
                        <Pencil className="h-3.5 w-3.5" />
                        Edit
                      </button>
                      <button
                        className="flex h-8 items-center gap-1.5 rounded-lg px-3 text-xs font-medium text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive"
                        disabled={busy}
                        onClick={(event) => {
                          event.stopPropagation();
                          setDeletingId(skill.id);
                        }}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                        Delete
                      </button>
                    </>
                  )}
                </div>
              </div>
            </article>
          ))}
        </div>

        {!filtered.length && (
          <div className="rounded-xl border border-dashed border-border py-16 text-center text-sm text-muted-foreground">
            {skills.length
              ? "No matching skills."
              : "No skills yet. Create one or import a skill folder."}
          </div>
        )}
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-2 text-xs font-semibold text-foreground">
      {label}
      {children}
    </label>
  );
}
