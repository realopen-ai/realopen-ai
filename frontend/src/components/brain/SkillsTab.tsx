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
import { t, useT } from "@/store/settingsStore";

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
  useT();
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
        cause instanceof Error ? cause.message : t("brain.skills.loadFailed"),
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
      setError(
        cause instanceof Error ? cause.message : t("brain.skills.openFailed"),
      );
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
      setError(
        cause instanceof Error ? cause.message : t("brain.skills.openFailed"),
      );
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
          : t("brain.skills.previewFailed"),
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
      setError(
        cause instanceof Error ? cause.message : t("brain.skills.saveFailed"),
      );
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
        cause instanceof Error ? cause.message : t("brain.skills.deleteFailed"),
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
        cause instanceof Error ? cause.message : t("brain.skills.importFailed"),
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
              aria-label={t("brain.skills.back")}
              className="-ml-2 mt-0.5"
              onClick={() => setSelectedSkill(null)}
            >
              <ChevronLeft />
              {t("brain.skills")}
            </Button>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2.5">
                <h2 className="text-[18px] font-semibold tracking-[-0.01em] text-foreground">
                  {selectedSkill.name}
                </h2>
                <StatusDot
                  tone={selectedSkill.enabled ? "success" : "neutral"}
                  label={
                    selectedSkill.enabled
                      ? t("brain.skills.enabled")
                      : t("brain.skills.disabled")
                  }
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
            {t("brain.skills.edit")}
          </Button>
        </div>

        <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_280px]">
          <main className="min-w-0">
            <SectionHeader
              title={t("brain.skills.instructions")}
              actions={
                <span className="font-mono text-[11px] text-muted-foreground/80">
                  SKILL.md
                </span>
              }
            />
            <div
              className="rounded-xl border border-border/60 bg-card p-6"
              dir="ltr"
            >
              <div className="prose prose-sm dark:prose-invert max-w-none text-left leading-relaxed">
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
              <SectionHeader title={t("brain.skills.availableTo")} />
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
                    {t(`brain.skills.role.${role}`)}
                  </span>
                ))}
              </div>
            </section>

            <section>
              <SectionHeader
                title={t("brain.skills.resources")}
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
                    <span className="text-xs">
                      {t("brain.skills.noResources")}
                    </span>
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
                  {t("brain.skills.closePreview")}
                </button>
              }
            />
            <pre
              className="max-h-128 overflow-auto rounded-xl border border-border/60 bg-sandbox-bg p-5 text-left font-mono text-xs leading-5"
              dir="ltr"
            >
              <HighlightedCode
                code={resourceContent ?? t("brain.skills.loadingResource")}
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
              {editingId ? t("brain.skills.edit") : t("brain.skills.create")}
            </h2>
            <p className="mt-1 text-[13.5px] text-muted-foreground">
              {t("brain.skills.savedAs")}
            </p>
          </div>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={t("brain.skills.closeEditor")}
            onClick={() => setDraft(null)}
          >
            <X />
          </Button>
        </div>

        <section className="grid gap-5 sm:grid-cols-2">
          <Field label={t("brain.skills.name")}>
            <input
              className="h-9 w-full rounded-lg border border-border/60 bg-transparent px-3 text-[13.5px] text-foreground outline-none transition-colors placeholder:text-muted-foreground/70 focus:border-primary/50 focus:ring-2 focus:ring-primary/20"
              value={draft.name}
              onChange={(event) =>
                setDraft({ ...draft, name: event.target.value })
              }
              placeholder={t("brain.skills.namePlaceholder")}
            />
          </Field>
          <Field label={t("brain.skills.descriptionLabel")}>
            <input
              className="h-9 w-full rounded-lg border border-border/60 bg-transparent px-3 text-[13.5px] text-foreground outline-none transition-colors placeholder:text-muted-foreground/70 focus:border-primary/50 focus:ring-2 focus:ring-primary/20"
              value={draft.description}
              onChange={(event) =>
                setDraft({ ...draft, description: event.target.value })
              }
              placeholder={t("brain.skills.descriptionPlaceholder")}
            />
          </Field>
        </section>

        <section>
          <Field label={t("brain.skills.availableTo")}>
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
                  {t(`brain.skills.role.${role}`)}
                </button>
              ))}
              <span className="ml-1.5 self-center text-xs text-muted-foreground/70">
                {draft.roles.length
                  ? t("brain.skills.selectedRoles")
                  : t("brain.skills.globalRoles")}
              </span>
            </div>
          </Field>
        </section>

        <Field label={t("brain.skills.instructionsMarkdown")}>
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
              {t("brain.skills.routingEnabled")}
            </div>
            <div className="mt-0.5 text-xs text-muted-foreground">
              {t("brain.skills.routingHelp")}
            </div>
          </div>
          <SettingToggle
            checked={draft.enabled}
            label={t("brain.skills.routingEnabled")}
            onChange={(v) => setDraft({ ...draft, enabled: v })}
          />
        </div>

        {error && <p className="text-[13px] text-danger">{error}</p>}
        <div className="sticky bottom-0 flex justify-end gap-2 border-t border-border/60 bg-background/95 py-4 backdrop-blur">
          <Button variant="ghost" onClick={() => setDraft(null)}>
            {t("workspace.cancel")}
          </Button>
          <Button
            disabled={busy || !draft.name.trim() || !draft.description.trim()}
            onClick={() => void save()}
          >
            {busy ? t("brain.skills.saving") : t("brain.skills.save")}
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
            placeholder={t("brain.skills.search")}
          />
        </div>
        <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
          {t(
            filtered.length === 1
              ? "brain.skills.count"
              : "brain.skills.countPlural",
            { count: filtered.length },
          )}
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
            <span className="hidden md:inline">
              {t("brain.skills.importZip")}
            </span>
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={busy}
            onClick={() => folderRef.current?.click()}
          >
            <FolderUp />
            <span className="hidden md:inline">
              {t("brain.skills.importFolder")}
            </span>
          </Button>
          <Button onClick={() => setDraft({ ...EMPTY })}>
            <Plus />
            <span className="hidden sm:inline">{t("brain.skills.new")}</span>
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
                      {t("brain.skills.editAction")}
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
                      {t("workspace.common.delete")}
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
                    {t("brain.skills.disabled")}
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
                    {t("brain.skills.deleteConfirm")}
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
                    {t("workspace.cancel")}
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
                    {t("workspace.common.delete")}
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
                          {t(`brain.skills.role.${role}`)}
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
          title={
            skills.length
              ? t("brain.skills.emptySearch")
              : t("brain.skills.empty")
          }
          description={
            skills.length ? undefined : t("brain.skills.emptyDescription")
          }
          action={
            <Button onClick={() => setDraft({ ...EMPTY })}>
              <Plus />
              {t("brain.skills.new")}
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
