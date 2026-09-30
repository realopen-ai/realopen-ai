import { useEffect, useMemo, useRef, useState } from "react";
import type { InputHTMLAttributes, ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { markdownCodeComponents } from "@/components/chat/MarkdownCodeBlock";
import { HighlightedCode } from "@/components/ui/HighlightedCode";
import {
  ArchiveRestore,
  ChevronLeft,
  Code2,
  FileText,
  Folder,
  FolderUp,
  Globe2,
  Mic2,
  MoreHorizontal,
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
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  EmptyState,
  SectionHeader,
  StatusDot,
} from "@/components/ui/primitives";
import { SettingToggle } from "@/components/brain/ToolsTab";
import { cn } from "@/lib/utils";

const ROLES: SkillRole[] = ["general", "coder", "voice"];
const EMPTY: SkillInput = {
  name: "",
  description: "",
  roles: [],
  enabled: true,
  content:
    "# Instructions\n\nDescribe when and how the agent should use this skill.\n",
};

const RoleIcon = ({ role }: { role: string }) =>
  role === "general" || role === "global" ? (
    <Globe2 className="h-3 w-3" />
  ) : role === "coder" ? (
    <Code2 className="h-3 w-3" />
  ) : (
    <Mic2 className="h-3 w-3" />
  );

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
      <div className="flex flex-col gap-7">
        {/* Header */}
        <div className="flex flex-wrap items-start justify-between gap-4 border-b border-border/60 pb-6">
          <div className="flex min-w-0 items-start gap-3.5">
            <Button
              variant="ghost"
              size="sm"
              aria-label="Back to skills"
              className="-ml-2 mt-0.5"
              onClick={() => setSelectedSkill(null)}
            >
              <ChevronLeft />
              Skills
            </Button>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2.5">
                <h2 className="text-[18px] font-semibold tracking-[-0.01em] text-foreground">
                  {selectedSkill.name}
                </h2>
                <StatusDot
                  tone={selectedSkill.enabled ? "success" : "neutral"}
                  label={selectedSkill.enabled ? "Enabled" : "Disabled"}
                />
              </div>
              <p className="mt-1 max-w-2xl text-[13.5px] leading-relaxed text-muted-foreground">
                {selectedSkill.description}
              </p>
            </div>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void edit(selectedSkill.id)}
          >
            <Pencil />
            Edit skill
          </Button>
        </div>

        <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_280px]">
          <main className="min-w-0">
            <SectionHeader
              title="Instructions"
              actions={
                <span className="font-mono text-[11px] text-muted-foreground/80">
                  SKILL.md
                </span>
              }
            />
            <div className="rounded-xl border border-border/60 bg-card p-6">
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

          <aside className="flex min-w-0 flex-col gap-8">
            <section>
              <SectionHeader title="Available to" />
              <div className="flex flex-wrap gap-1.5">
                {(selectedSkill.roles.length
                  ? selectedSkill.roles
                  : ["global"]
                ).map((role) => (
                  <span
                    key={role}
                    className="inline-flex h-6 items-center gap-1.5 rounded-md bg-secondary px-2 text-[11.5px] font-medium capitalize text-muted-foreground"
                  >
                    <RoleIcon role={role} />
                    {role}
                  </span>
                ))}
              </div>
            </section>

            <section>
              <SectionHeader
                title="Resources"
                actions={
                  <span className="text-xs text-muted-foreground tabular-nums">
                    {resources.length}
                  </span>
                }
              />
              <div className="divide-y divide-border/50 overflow-hidden rounded-xl border border-border/60 bg-card">
                {resources.length ? (
                  resources.map((path) => (
                    <button
                      key={path}
                      className={cn(
                        "flex w-full items-center gap-2.5 px-3 py-2.5 text-left text-xs transition-colors",
                        selectedResource === path
                          ? "bg-primary/10 text-primary"
                          : "text-muted-foreground hover:bg-surface-hover hover:text-foreground",
                      )}
                      onClick={() => void viewResource(path)}
                    >
                      <FileText className="h-3.5 w-3.5 shrink-0" />
                      <span className="min-w-0 truncate font-mono">{path}</span>
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
            <SectionHeader
              title={
                <span className="truncate font-mono text-[13px] font-medium">
                  {selectedResource}
                </span>
              }
              actions={
                <button
                  className="text-xs text-muted-foreground transition-colors hover:text-foreground"
                  onClick={() => {
                    setSelectedResource(null);
                    setResourceContent(null);
                  }}
                >
                  Close preview
                </button>
              }
            />
            <pre className="max-h-128 overflow-auto rounded-xl border border-border/60 bg-sandbox-bg p-5 font-mono text-xs leading-5">
              <HighlightedCode
                code={resourceContent ?? "Loading resource…"}
                filePath={selectedResource}
              />
            </pre>
          </section>
        )}
      </div>
    );
  }

  if (draft) {
    return (
      <div className="flex flex-col gap-7 pb-2">
        {/* Header */}
        <div className="flex items-start justify-between gap-4 border-b border-border/60 pb-6">
          <div className="min-w-0">
            <h2 className="text-[18px] font-semibold tracking-[-0.01em] text-foreground">
              {editingId ? "Edit skill" : "Create skill"}
            </h2>
            <p className="mt-1 text-[13.5px] text-muted-foreground">
              Saved as a SKILL.md file with YAML metadata.
            </p>
          </div>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Close editor"
            onClick={() => setDraft(null)}
          >
            <X />
          </Button>
        </div>

        <section className="grid gap-5 sm:grid-cols-2">
          <Field label="Name">
            <input
              className="h-9 w-full rounded-lg border border-border/60 bg-transparent px-3 text-[13.5px] text-foreground outline-none transition-colors placeholder:text-muted-foreground/70 focus:border-primary/50 focus:ring-2 focus:ring-primary/20"
              value={draft.name}
              onChange={(event) =>
                setDraft({ ...draft, name: event.target.value })
              }
              placeholder="Code review"
            />
          </Field>
          <Field label="Description">
            <input
              className="h-9 w-full rounded-lg border border-border/60 bg-transparent px-3 text-[13.5px] text-foreground outline-none transition-colors placeholder:text-muted-foreground/70 focus:border-primary/50 focus:ring-2 focus:ring-primary/20"
              value={draft.description}
              onChange={(event) =>
                setDraft({ ...draft, description: event.target.value })
              }
              placeholder="Review changes for correctness and regressions"
            />
          </Field>
        </section>

        <section>
          <Field label="Available to">
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              {ROLES.map((role) => (
                <button
                  key={role}
                  type="button"
                  className={cn(
                    "inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[12.5px] font-medium capitalize transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60",
                    draft.roles.includes(role)
                      ? "bg-primary/10 text-primary"
                      : "text-muted-foreground hover:bg-surface-hover hover:text-foreground",
                  )}
                  onClick={() =>
                    setDraft({
                      ...draft,
                      roles: draft.roles.includes(role)
                        ? draft.roles.filter((item) => item !== role)
                        : [...draft.roles, role],
                    })
                  }
                >
                  <RoleIcon role={role} />
                  {role}
                </button>
              ))}
              <span className="ml-1.5 self-center text-xs text-muted-foreground/70">
                {draft.roles.length
                  ? "Selected roles only"
                  : "Global (all roles)"}
              </span>
            </div>
          </Field>
        </section>

        <Field label="Instructions (Markdown)">
          <textarea
            className="min-h-80 w-full resize-y rounded-xl border border-border/60 bg-card p-4 font-mono text-[13px] leading-6 text-foreground outline-none transition-colors focus:border-primary/50 focus:ring-2 focus:ring-primary/20"
            value={draft.content}
            onChange={(event) =>
              setDraft({ ...draft, content: event.target.value })
            }
          />
        </Field>

        <div className="flex items-center gap-3 rounded-xl bg-secondary/60 px-4 py-3">
          <div className="flex-1">
            <div className="text-[13.5px] text-foreground">
              Enabled for agent routing
            </div>
            <div className="mt-0.5 text-xs text-muted-foreground">
              Disabled skills stay on disk but are never offered to agents.
            </div>
          </div>
          <SettingToggle
            checked={draft.enabled}
            label="Enabled for agent routing"
            onChange={(v) => setDraft({ ...draft, enabled: v })}
          />
        </div>

        {error && <p className="text-[13px] text-danger">{error}</p>}
        <div className="sticky bottom-0 flex justify-end gap-2 border-t border-border/60 bg-background/95 py-4 backdrop-blur">
          <Button variant="ghost" onClick={() => setDraft(null)}>
            Cancel
          </Button>
          <Button
            disabled={busy || !draft.name.trim() || !draft.description.trim()}
            onClick={() => void save()}
          >
            {busy ? "Saving…" : "Save skill"}
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-5">
      {/* Toolbar: search + actions */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative w-full sm:max-w-xs">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground/80" />
          <input
            className="h-9 w-full rounded-lg border border-border/60 bg-transparent pl-9 pr-3 text-[13px] text-foreground outline-none transition-colors placeholder:text-muted-foreground/80 focus:border-primary/50 focus:ring-2 focus:ring-primary/20"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search skills…"
          />
        </div>
        <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
          {filtered.length} {filtered.length === 1 ? "skill" : "skills"}
        </span>
        <div className="flex-1" />
        <div className="flex items-center gap-2">
          <Button
            variant="secondary"
            size="sm"
            disabled={busy}
            onClick={() => archiveRef.current?.click()}
          >
            <ArchiveRestore />
            <span className="hidden md:inline">Import ZIP</span>
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={busy}
            onClick={() => folderRef.current?.click()}
          >
            <FolderUp />
            <span className="hidden md:inline">Import folder</span>
          </Button>
          <Button onClick={() => setDraft({ ...EMPTY })}>
            <Plus />
            <span className="hidden sm:inline">New skill</span>
          </Button>
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

      {error && <p className="text-[13px] text-danger">{error}</p>}

      {/* Skill cards */}
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {filtered.map((skill) => (
          <article
            key={skill.id}
            role="button"
            tabIndex={0}
            className="group relative flex min-h-35 cursor-pointer flex-col rounded-xl border border-border/60 bg-card p-4 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring/60 hover:bg-surface-hover"
            onClick={() => void view(skill.id)}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ")
                void view(skill.id);
            }}
          >
            {/* Hover action menu */}
            {deletingId !== skill.id && (
              <div className="absolute right-2.5 top-2.5 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
                <DropdownMenu>
                  <DropdownMenuTrigger
                    aria-label={`Actions for ${skill.name}`}
                    className="flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60 data-[state=open]:bg-secondary data-[state=open]:text-foreground"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <MoreHorizontal className="h-4 w-4" />
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    <DropdownMenuItem
                      disabled={busy}
                      onClick={(e) => {
                        e.stopPropagation();
                        void edit(skill.id);
                      }}
                    >
                      <Pencil />
                      Edit
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      className="text-danger focus:text-danger [&_svg]:text-danger"
                      disabled={busy}
                      onClick={(e) => {
                        e.stopPropagation();
                        setDeletingId(skill.id);
                      }}
                    >
                      <Trash2 />
                      Delete
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
            )}

            <div className="min-w-0 pr-7">
              <div className="flex items-center gap-2">
                <h3 className="truncate text-[14px] font-medium text-foreground">
                  {skill.name}
                </h3>
                {!skill.enabled && (
                  <span className="shrink-0 rounded-md bg-secondary px-1.5 py-0.5 text-[10.5px] font-medium text-muted-foreground">
                    Disabled
                  </span>
                )}
              </div>
              <p className="mt-1 line-clamp-2 text-[13px] leading-relaxed text-muted-foreground">
                {skill.description}
              </p>
            </div>

            <div className="mt-auto pt-4">
              {deletingId === skill.id ? (
                <div className="flex items-center justify-end gap-2 border-t border-border/60 pt-3">
                  <span className="mr-auto text-xs text-muted-foreground">
                    Delete this skill?
                  </span>
                  <Button
                    variant="ghost"
                    size="xs"
                    disabled={busy}
                    onClick={(event) => {
                      event.stopPropagation();
                      setDeletingId(null);
                    }}
                  >
                    Cancel
                  </Button>
                  <Button
                    variant="destructive"
                    size="xs"
                    disabled={busy}
                    onClick={(event) => {
                      event.stopPropagation();
                      void remove(skill);
                    }}
                  >
                    Delete
                  </Button>
                </div>
              ) : (
                <div className="flex items-center justify-between gap-3 text-[11px] text-muted-foreground">
                  <div className="flex flex-wrap gap-1.5">
                    {(skill.roles.length ? skill.roles : ["global"]).map(
                      (role) => (
                        <span
                          key={role}
                          className="inline-flex h-5 items-center gap-1 rounded-md bg-secondary px-1.5 font-medium capitalize"
                        >
                          <RoleIcon role={role} />
                          {role}
                        </span>
                      ),
                    )}
                  </div>
                  <span className="flex shrink-0 items-center gap-1 tabular-nums">
                    <FileText className="h-3 w-3" />
                    {Math.max(skill.files.length - 1, 0)}
                  </span>
                </div>
              )}
            </div>
          </article>
        ))}
      </div>

      {/* Empty state */}
      {!filtered.length && (
        <EmptyState
          icon={<Code2 />}
          title={skills.length ? "No matching skills." : "No skills yet"}
          description={
            skills.length
              ? undefined
              : "Create one or import a skill folder — reusable instructions your agents can load on demand."
          }
          action={
            <Button onClick={() => setDraft({ ...EMPTY })}>
              <Plus />
              New skill
            </Button>
          }
        />
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5 text-[12.5px] font-medium text-foreground">
      {label}
      {children}
    </label>
  );
}
