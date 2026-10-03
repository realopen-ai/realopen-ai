import { useState, useEffect, useCallback, useRef } from "react";
import {
  Plus,
  FileText,
  Trash2,
  Check,
  Loader2,
  AlertCircle,
  Upload,
  X,
  Pencil,
  MoreHorizontal,
} from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/primitives";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import { t, useT } from "@/store/settingsStore";

interface Template {
  id: string;
  display_name: string;
  slug: string;
  description: string | null;
  tags: string[];
  thumbnail: string | null;
  path: string;
  created_at: number;
  updated_at: number;
}

type FormMode = "view" | "edit" | "add";

const fieldLabelClass = "mb-1.5 block text-xs font-medium text-foreground";
const fieldInputClass =
  "h-9 w-full rounded-lg border border-border/60 bg-transparent px-3 text-[13.5px] text-foreground outline-none transition-colors focus:border-primary/50 placeholder:text-muted-foreground/70";

export function TemplatesSection() {
  useT();
  const [templates, setTemplates] = useState<Template[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [formMode, setFormMode] = useState<FormMode>("view");

  const loadTemplates = useCallback(async () => {
    setIsLoading(true);
    try {
      const res = await fetch("/api/workspace/templates");
      if (res.ok) {
        const data = await res.json();
        setTemplates(data.templates ?? []);
      }
    } catch {
      /* ignore */
    }
    setIsLoading(false);
  }, []);

  useEffect(() => {
    loadTemplates();
  }, [loadTemplates]);

  const handleSelect = (id: string) => {
    setSelectedId(id);
    setFormMode("view");
  };

  const handleAdd = () => {
    setSelectedId(null);
    setFormMode("add");
  };

  const handleEdit = (id: string) => {
    setSelectedId(id);
    setFormMode("edit");
  };

  const handleDelete = async (id: string) => {
    try {
      await fetch(`/api/workspace/templates/${id}`, { method: "DELETE" });
      if (selectedId === id) {
        setSelectedId(null);
        setFormMode("view");
      }
      loadTemplates();
    } catch {
      /* ignore */
    }
  };

  const selectedTemplate = templates.find((t) => t.id === selectedId);

  return (
    <div className="flex h-full">
      {/* Left: grid */}
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="shrink-0 px-6 pt-5 lg:px-10">
          <div className="mx-auto flex w-full max-w-300 items-center justify-between gap-3">
            <p className="text-xs text-muted-foreground">
              {t(templates.length === 1 ? "workspace.templates.count" : "workspace.templates.countPlural", { count: templates.length })}
            </p>
            <Button size="sm" onClick={handleAdd}>
              <Plus />
              {t("workspace.templates.add")}
            </Button>
          </div>
        </div>
        <ScrollArea className="min-h-0 flex-1">
          <div className="mx-auto w-full max-w-300 px-6 pb-16 pt-4 lg:px-10">
            {isLoading ? (
              <div className="flex items-center justify-center py-16 text-muted-foreground">
                <Loader2 className="h-5 w-5 animate-spin" />
              </div>
            ) : templates.length === 0 ? (
              <EmptyState
                icon={<FileText />}
                title={t("workspace.templates.empty")}
                description={t("workspace.templates.emptyDescription")}
                action={
                  <Button size="sm" onClick={handleAdd}>
                    <Plus />
                    {t("workspace.templates.add")}
                  </Button>
                }
                className="rounded-xl"
              />
            ) : (
              <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
                {templates.map((tpl) => (
                  <div
                    key={tpl.id}
                    className={cn(
                      "group relative overflow-hidden rounded-xl border text-left transition-colors",
                      selectedId === tpl.id
                        ? "border-primary/40 bg-primary/4"
                        : "border-border/60 bg-card hover:bg-surface-hover",
                    )}
                  >
                    <button
                      onClick={() => handleSelect(tpl.id)}
                      className="block w-full text-left focus-visible:outline-none"
                      aria-label={`Open ${tpl.display_name}`}
                    >
                      <div className="aspect-4/3 overflow-hidden bg-secondary">
                        {tpl.thumbnail ? (
                          <img
                            src={`data:image/jpeg;base64,${tpl.thumbnail}`}
                            alt={tpl.display_name}
                            className="h-full w-full object-cover"
                          />
                        ) : (
                          <div className="flex h-full items-center justify-center">
                            <FileText className="h-7 w-7 text-muted-foreground/70" />
                          </div>
                        )}
                      </div>
                      <div className="p-3.5">
                        <p className="truncate text-[13.5px] font-medium text-foreground">
                          {tpl.display_name}
                        </p>
                        <p className="mt-0.5 truncate text-xs text-muted-foreground">
                          {tpl.description || t("workspace.templates.noDescription")}
                        </p>
                      </div>
                    </button>

                    {/* Hover actions */}
                    <div className="absolute right-1.5 top-1.5 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            aria-label={`Actions for ${tpl.display_name}`}
                            className="bg-background/80 backdrop-blur-sm"
                          >
                            <MoreHorizontal />
                          </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end">
                          <DropdownMenuItem onSelect={() => handleEdit(tpl.id)}>
                            <Pencil />
                            {t("workspace.templates.edit")}
                          </DropdownMenuItem>
                          <DropdownMenuSeparator />
                          <DropdownMenuItem
                            className="text-danger focus:text-danger [&_svg]:text-danger"
                            onSelect={() => {
                              if (
                                confirm(
                                  t("workspace.templates.deleteConfirm", { name: tpl.display_name }),
                                )
                              )
                                handleDelete(tpl.id);
                            }}
                          >
                            <Trash2 />
                            {t("workspace.templates.delete")}
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </ScrollArea>
      </div>

      {/* Right: split panel (view/edit/add) */}
      {(selectedTemplate || formMode === "add") && (
        <div className="flex w-85 shrink-0 flex-col border-l border-border/60 bg-card">
          {formMode === "add" ? (
            <TemplateForm
              mode="add"
              onSaved={() => {
                setFormMode("view");
                setSelectedId(null);
                loadTemplates();
              }}
              onCancel={() => {
                setFormMode("view");
                setSelectedId(null);
              }}
            />
          ) : selectedTemplate ? (
            formMode === "edit" ? (
              <TemplateForm
                mode="edit"
                template={selectedTemplate}
                onSaved={() => {
                  setFormMode("view");
                  loadTemplates();
                }}
                onCancel={() => setFormMode("view")}
              />
            ) : (
              <TemplateDetail
                template={selectedTemplate}
                onEdit={() => handleEdit(selectedTemplate.id)}
                onDelete={() => handleDelete(selectedTemplate.id)}
              />
            )
          ) : null}
        </div>
      )}
    </div>
  );
}

// ─── Template Detail (read-only view) ────────────────────────────────

function TemplateDetail({
  template,
  onEdit,
  onDelete,
}: {
  template: Template;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  return (
    <div className="flex h-full flex-col">
      <div className="flex h-12 shrink-0 items-center justify-between border-b border-border/60 px-4">
        <span className="text-[14px] font-medium text-foreground">
          {t("workspace.templates.details")}
        </span>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onEdit}
            aria-label={t("workspace.templates.edit")}
            title={t("workspace.templates.edit")}
          >
            <Pencil />
          </Button>
          {confirmDelete ? (
            <div className="flex items-center gap-1">
              <button
                onClick={onDelete}
                className="rounded-md bg-danger/10 px-2 py-1 text-[11.5px] font-medium text-danger transition-colors hover:bg-danger/20"
              >
                {t("workspace.common.delete")}
              </button>
              <Button
                variant="ghost"
                size="xs"
                onClick={() => setConfirmDelete(false)}
              >
                {t("workspace.cancel")}
              </Button>
            </div>
          ) : (
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => setConfirmDelete(true)}
              aria-label={t("workspace.templates.delete")}
              title={t("workspace.common.delete")}
              className="text-muted-foreground/80 hover:text-danger hover:bg-danger/10"
            >
              <Trash2 />
            </Button>
          )}
        </div>
      </div>
      <ScrollArea className="min-h-0 flex-1">
        <div className="space-y-5 p-4">
          <div className="aspect-4/3 overflow-hidden rounded-lg bg-secondary">
            {template.thumbnail ? (
              <img
                src={`data:image/jpeg;base64,${template.thumbnail}`}
                alt={template.display_name}
                className="h-full w-full object-cover"
              />
            ) : (
              <div className="flex h-full items-center justify-center">
                <FileText className="h-10 w-10 text-muted-foreground/30" />
              </div>
            )}
          </div>
          <div>
            <label className="text-xs font-medium text-muted-foreground">
              {t("workspace.templates.name")}
            </label>
            <p className="mt-0.5 text-[13.5px] font-medium text-foreground">
              {template.display_name}
            </p>
          </div>
          <div>
            <label className="text-xs font-medium text-muted-foreground">
              {t("workspace.templates.slug")}
            </label>
            <p className="mt-0.5 font-mono text-[12.5px] text-muted-foreground">
              {template.slug}
            </p>
          </div>
          {template.description && (
            <div>
              <label className="text-xs font-medium text-muted-foreground">
                {t("workspace.templates.descriptionLabel")}
              </label>
              <p className="mt-0.5 text-[13px] text-foreground/80">
                {template.description}
              </p>
            </div>
          )}
          {template.tags.length > 0 && (
            <div>
              <label className="text-xs font-medium text-muted-foreground">
                {t("workspace.templates.tags")}
              </label>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {template.tags.map((tag) => (
                  <span
                    key={tag}
                    className="rounded-full bg-primary/10 px-2 py-0.5 text-[11.5px] font-medium text-primary"
                  >
                    {tag}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      </ScrollArea>
    </div>
  );
}

// ─── Template Form (add/edit with validation flow) ───────────────────

function TemplateForm({
  mode,
  template,
  onSaved,
  onCancel,
}: {
  mode: "add" | "edit";
  template?: Template;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [displayName, setDisplayName] = useState(template?.display_name ?? "");
  const [description, setDescription] = useState(template?.description ?? "");
  const [tags, setTags] = useState((template?.tags ?? []).join(", "));
  const [thumbnail, setThumbnail] = useState<string | null>(
    template?.thumbnail ?? null,
  );
  const [, setThumbnailName] = useState<string>("");
  const [file, setFile] = useState<File | null>(null);
  const [validationState, setValidationState] = useState<
    "idle" | "validating" | "valid" | "invalid"
  >("idle");
  const [validationError, setValidationError] = useState("");
  const [validationDetails, setValidationDetails] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const thumbInputRef = useRef<HTMLInputElement>(null);

  const handleThumbnailUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    setThumbnailName(f.name);
    const reader = new FileReader();
    reader.onload = () => {
      // Resize the image to 200x150 using canvas
      const img = new Image();
      img.onload = () => {
        const canvas = document.createElement("canvas");
        canvas.width = 200;
        canvas.height = 150;
        const ctx = canvas.getContext("2d");
        if (ctx) {
          ctx.drawImage(img, 0, 0, 200, 150);
          const b64 = canvas.toDataURL("image/jpeg", 0.85).split(",")[1];
          setThumbnail(b64);
        }
      };
      img.src = reader.result as string;
    };
    reader.readAsDataURL(f);
  };

  const handleValidate = async () => {
    if (mode === "add" && !file) {
      setValidationState("invalid");
      setValidationError(t("workspace.templates.selectPptx"));
      return;
    }

    setValidationState("validating");
    setValidationError("");
    setValidationDetails("");

    try {
      if (mode === "add" && file) {
        const formData = new FormData();
        formData.append("file", file);
        const res = await fetch("/api/workspace/templates/validate", {
          method: "POST",
          body: formData,
        });
        const data = await res.json();
        if (data.valid) {
          setValidationState("valid");
          setValidationDetails(
            `Valid: ${data.details?.layouts} layouts, ${data.details?.slide_width} × ${data.details?.slide_height}, ratio ${data.details?.aspect_ratio}`,
          );
        } else {
          setValidationState("invalid");
          setValidationError(data.error || t("workspace.templates.validationFailed"));
        }
      } else if (mode === "edit") {
        // For edit mode, validate the existing file from the backend
        // We can't re-upload the file — just mark as valid since it was already validated
        setValidationState("valid");
        setValidationDetails(t("workspace.templates.alreadyValidated"));
      }
    } catch (e) {
      setValidationState("invalid");
      setValidationError(t("workspace.templates.networkError", { error: String(e) }));
    }
  };

  const handleSave = async () => {
    setIsSaving(true);
    try {
      if (mode === "add") {
        const formData = new FormData();
        formData.append("display_name", displayName);
        formData.append("description", description);
        formData.append("tags", tags);
        if (thumbnail) formData.append("thumbnail", thumbnail);
        if (file) formData.append("file", file);

        const res = await fetch("/api/workspace/templates", {
          method: "POST",
          body: formData,
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${res.status}`);
        }
      } else if (mode === "edit" && template) {
        const formData = new FormData();
        if (displayName !== template.display_name)
          formData.append("display_name", displayName);
        if (description !== (template.description ?? ""))
          formData.append("description", description);
        if (tags !== (template.tags ?? []).join(", "))
          formData.append("tags", tags);
        if (thumbnail !== (template.thumbnail ?? null))
          formData.append("thumbnail", thumbnail ?? "");

        const res = await fetch(`/api/workspace/templates/${template.id}`, {
          method: "PUT",
          body: formData,
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${res.status}`);
        }
      }
      onSaved();
    } catch (e) {
      setValidationState("invalid");
      setValidationError(t("workspace.templates.saveFailed", { error: String(e) }));
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-12 shrink-0 items-center justify-between border-b border-border/60 px-4">
        <span className="text-[14px] font-medium text-foreground">
          {mode === "add" ? t("workspace.templates.add") : t("workspace.templates.edit")}
        </span>
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={onCancel}
          aria-label={t("common.close")}
        >
          <X />
        </Button>
      </div>
      <ScrollArea className="min-h-0 flex-1">
        <div className="space-y-4 p-4">
          {/* PPTX file upload (add mode only) */}
          {mode === "add" && (
            <div>
              <label className={fieldLabelClass}>{t("workspace.templates.pptxFile")}</label>
              <input
                ref={fileInputRef}
                type="file"
                accept=".pptx"
                onChange={(e) => {
                  setFile(e.target.files?.[0] ?? null);
                  setValidationState("idle");
                }}
                className="hidden"
              />
              <button
                onClick={() => fileInputRef.current?.click()}
                className={cn(
                  "flex w-full items-center gap-2 rounded-lg border px-3 py-2.5 text-[12.5px] transition-colors",
                  file
                    ? "border-primary/40 bg-primary/5 text-foreground"
                    : "border-dashed border-border/70 text-muted-foreground hover:border-primary/40 hover:text-foreground",
                )}
              >
                <Upload className="h-4 w-4 shrink-0" />
                <span className="truncate">
                  {file ? file.name : t("workspace.templates.uploadPptx")}
                </span>
              </button>
            </div>
          )}

          {/* Display Name */}
          <div>
            <label className={fieldLabelClass}>{t("workspace.templates.displayName")}</label>
            <input
              type="text"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder={t("workspace.templates.namePlaceholder")}
              className={fieldInputClass}
            />
          </div>

          {/* Description */}
          <div>
            <label className={fieldLabelClass}>{t("workspace.templates.descriptionLabel")}</label>
            <input
              type="text"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t("workspace.templates.descriptionPlaceholder")}
              className={fieldInputClass}
            />
          </div>

          {/* Tags */}
          <div>
            <label className={fieldLabelClass}>{t("workspace.templates.tagsOptional")}</label>
            <input
              type="text"
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              placeholder={t("workspace.templates.tagsPlaceholder")}
              className={fieldInputClass}
            />
            <p className="mt-1 text-[11px] text-muted-foreground">
              {t("workspace.templates.tagsHelp")}
            </p>
          </div>

          {/* Thumbnail */}
          <div>
            <label className={fieldLabelClass}>{t("workspace.templates.thumbnailOptional")}</label>
            <input
              ref={thumbInputRef}
              type="file"
              accept="image/*"
              onChange={handleThumbnailUpload}
              className="hidden"
            />
            <div className="flex items-center gap-2">
              <div className="flex h-12 w-16 shrink-0 items-center justify-center overflow-hidden rounded-md bg-secondary">
                {thumbnail ? (
                  <img
                    src={`data:image/jpeg;base64,${thumbnail}`}
                    alt={t("workspace.templates.thumbnail")}
                    className="h-full w-full object-cover"
                  />
                ) : (
                  <FileText className="h-5 w-5 text-muted-foreground/30" />
                )}
              </div>
              <Button
                variant="outline"
                size="xs"
                onClick={() => thumbInputRef.current?.click()}
              >
                {thumbnail ? t("workspace.templates.change") : t("workspace.templates.upload")}
              </Button>
              {thumbnail && (
                <Button
                  variant="ghost"
                  size="xs"
                  onClick={() => {
                    setThumbnail(null);
                    setThumbnailName("");
                  }}
                  className="text-muted-foreground hover:text-danger"
                >
                  {t("workspace.templates.remove")}
                </Button>
              )}
            </div>
          </div>

          {/* Validation */}
          <div className="space-y-2">
            <Button
              variant="outline"
              className={cn(
                "w-full",
                validationState === "validating" &&
                  "cursor-wait text-muted-foreground",
                validationState === "valid" &&
                  "border-success/30 bg-success/10 text-success hover:bg-success/15",
                validationState === "invalid" &&
                  "border-danger/30 bg-danger/10 text-danger hover:bg-danger/15",
              )}
              onClick={() => void handleValidate()}
              disabled={
                validationState === "validating" || (mode === "add" && !file)
              }
            >
              {validationState === "validating" ? (
                <>
                  <Loader2 className="animate-spin" /> {t("workspace.templates.validating")}
                </>
              ) : validationState === "valid" ? (
                <>
                  <Check /> {t("workspace.templates.validated")}
                </>
              ) : validationState === "invalid" ? (
                <>
                  <AlertCircle /> {t("workspace.templates.validationRetry")}
                </>
              ) : (
                <>
                  <Upload /> {t("workspace.templates.test")}
                </>
              )}
            </Button>

            {validationState === "valid" && validationDetails && (
              <p className="px-1 text-[11px] text-success/80">
                {validationDetails}
              </p>
            )}
            {validationState === "invalid" && validationError && (
              <div className="rounded-lg border border-danger/25 bg-danger/5 px-3 py-2">
                <p className="text-[11.5px] text-danger">{validationError}</p>
                <p className="mt-1 text-[11px] text-danger/70">
                  {t("workspace.templates.fixRetry")}
                </p>
              </div>
            )}
          </div>

          {/* Save button — only after successful validation */}
          {validationState === "valid" && (
            <Button
              className="w-full"
              onClick={() => void handleSave()}
              disabled={isSaving || !displayName.trim()}
            >
              {isSaving ? <Loader2 className="animate-spin" /> : <Check />}
              {mode === "add" ? t("workspace.templates.add") : t("workspace.templates.saveChanges")}
            </Button>
          )}
        </div>
      </ScrollArea>
    </div>
  );
}
