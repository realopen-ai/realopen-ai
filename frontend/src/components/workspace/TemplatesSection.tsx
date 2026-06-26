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
} from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";

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

export function TemplatesSection() {
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
      <div className="flex-1 min-w-0 flex flex-col">
        <div className="flex items-center justify-between px-4 pt-4 pb-3">
          <p className="text-[13px] text-muted-foreground">
            {templates.length} template(s)
          </p>
          <button
            onClick={handleAdd}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[12px] font-medium bg-primary/10 text-primary hover:bg-primary/20 transition-colors"
          >
            <Plus className="w-3.5 h-3.5" />
            Add Template
          </button>
        </div>
        <ScrollArea className="flex-1 px-4">
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3 pb-4">
            {isLoading ? (
              <div className="col-span-full flex items-center justify-center py-12">
                <Loader2 className="w-5 h-5 text-primary animate-spin" />
              </div>
            ) : templates.length === 0 ? (
              <div className="col-span-full flex flex-col items-center justify-center py-12">
                <FileText className="w-8 h-8 text-muted-foreground/20 mb-2" />
                <p className="text-[13px] text-muted-foreground/60">
                  No templates yet. Click "Add Template" to upload one.
                </p>
              </div>
            ) : (
              templates.map((tpl) => (
                <button
                  key={tpl.id}
                  onClick={() => handleSelect(tpl.id)}
                  className={cn(
                    "flex flex-col items-center gap-2 p-3 rounded-xl border transition-all text-left",
                    selectedId === tpl.id
                      ? "border-primary bg-primary/5"
                      : "border-border hover:border-primary/30 hover:bg-accent/30",
                  )}
                >
                  <div className="w-full aspect-video rounded-lg bg-secondary flex items-center justify-center overflow-hidden">
                    {tpl.thumbnail ? (
                      <img
                        src={`data:image/jpeg;base64,${tpl.thumbnail}`}
                        alt={tpl.display_name}
                        className="w-full h-full object-cover"
                      />
                    ) : (
                      <FileText className="w-8 h-8 text-muted-foreground/30" />
                    )}
                  </div>
                  <div className="w-full">
                    <p className="text-[12px] font-medium text-foreground truncate">
                      {tpl.display_name}
                    </p>
                    <p className="text-[10px] text-muted-foreground/60 truncate">
                      {tpl.description || "No description"}
                    </p>
                  </div>
                </button>
              ))
            )}
          </div>
        </ScrollArea>
      </div>

      {/* Right: split panel (view/edit/add) */}
      {(selectedTemplate || formMode === "add") && (
        <div className="w-85 shrink-0 border-l border-border/50 flex flex-col bg-card">
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
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-4 py-3 border-b border-border/50">
        <span className="text-[13px] font-medium">Template Details</span>
        <div className="flex gap-1">
          <button
            onClick={onEdit}
            className="w-7 h-7 rounded-lg flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
          >
            <Pencil className="w-3.5 h-3.5" />
          </button>
          {confirmDelete ? (
            <div className="flex items-center gap-1">
              <button
                onClick={onDelete}
                className="px-2 py-1 rounded text-[11px] bg-destructive/10 text-destructive hover:bg-destructive/20 font-medium"
              >
                Delete
              </button>
              <button
                onClick={() => setConfirmDelete(false)}
                className="px-2 py-1 rounded text-[11px] text-muted-foreground hover:bg-accent"
              >
                Cancel
              </button>
            </div>
          ) : (
            <button
              onClick={() => setConfirmDelete(true)}
              className="w-7 h-7 rounded-lg flex items-center justify-center text-muted-foreground/50 hover:text-destructive hover:bg-destructive/10 transition-colors"
            >
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
      </div>
      <ScrollArea className="flex-1">
        <div className="p-4 space-y-4">
          <div className="aspect-video rounded-lg bg-secondary flex items-center justify-center overflow-hidden">
            {template.thumbnail ? (
              <img
                src={`data:image/jpeg;base64,${template.thumbnail}`}
                alt={template.display_name}
                className="w-full h-full object-cover"
              />
            ) : (
              <FileText className="w-10 h-10 text-muted-foreground/30" />
            )}
          </div>
          <div>
            <label className="text-[11px] text-muted-foreground/60 uppercase tracking-wide">
              Name
            </label>
            <p className="text-[13px] text-foreground font-medium mt-0.5">
              {template.display_name}
            </p>
          </div>
          <div>
            <label className="text-[11px] text-muted-foreground/60 uppercase tracking-wide">
              Slug
            </label>
            <p className="text-[12px] text-muted-foreground font-mono mt-0.5">
              {template.slug}
            </p>
          </div>
          {template.description && (
            <div>
              <label className="text-[11px] text-muted-foreground/60 uppercase tracking-wide">
                Description
              </label>
              <p className="text-[12px] text-foreground/80 mt-0.5">
                {template.description}
              </p>
            </div>
          )}
          {template.tags.length > 0 && (
            <div>
              <label className="text-[11px] text-muted-foreground/60 uppercase tracking-wide">
                Tags
              </label>
              <div className="flex flex-wrap gap-1.5 mt-1">
                {template.tags.map((tag) => (
                  <span
                    key={tag}
                    className="px-2 py-0.5 rounded-full text-[11px] bg-primary/10 text-primary"
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
      setValidationError("Please select a PPTX file first.");
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
          setValidationError(data.error || "Validation failed");
        }
      } else if (mode === "edit") {
        // For edit mode, validate the existing file from the backend
        // We can't re-upload the file — just mark as valid since it was already validated
        setValidationState("valid");
        setValidationDetails("Existing template — already validated.");
      }
    } catch (e) {
      setValidationState("invalid");
      setValidationError(`Network error: ${e}`);
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
      setValidationError(`Save failed: ${e}`);
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-4 py-3 border-b border-border/50">
        <span className="text-[13px] font-medium">
          {mode === "add" ? "Add Template" : "Edit Template"}
        </span>
        <button
          onClick={onCancel}
          className="w-7 h-7 rounded-lg flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
        >
          <X className="w-4 h-4" />
        </button>
      </div>
      <ScrollArea className="flex-1">
        <div className="p-4 space-y-4">
          {/* PPTX file upload (add mode only) */}
          {mode === "add" && (
            <div>
              <label className="text-[11px] text-muted-foreground/60 uppercase tracking-wide block mb-1.5">
                PPTX File
              </label>
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
                  "w-full flex items-center gap-2 px-3 py-2.5 rounded-lg border text-[12px] transition-colors",
                  file
                    ? "border-primary/30 bg-primary/5 text-foreground"
                    : "border-dashed border-border text-muted-foreground hover:border-primary/30 hover:text-foreground",
                )}
              >
                <Upload className="w-4 h-4 shrink-0" />
                <span className="truncate">
                  {file ? file.name : "Click to upload .pptx file"}
                </span>
              </button>
            </div>
          )}

          {/* Display Name */}
          <div>
            <label className="text-[11px] text-muted-foreground/60 uppercase tracking-wide block mb-1.5">
              Display Name
            </label>
            <input
              type="text"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="e.g. Research Template"
              className="w-full px-3 py-2 rounded-lg border border-border bg-background text-[13px] text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:ring-1 focus:ring-primary"
            />
          </div>

          {/* Description */}
          <div>
            <label className="text-[11px] text-muted-foreground/60 uppercase tracking-wide block mb-1.5">
              Description
            </label>
            <input
              type="text"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="1-4 words (e.g. Navy professional)"
              className="w-full px-3 py-2 rounded-lg border border-border bg-background text-[13px] text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:ring-1 focus:ring-primary"
            />
          </div>

          {/* Tags */}
          <div>
            <label className="text-[11px] text-muted-foreground/60 uppercase tracking-wide block mb-1.5">
              Tags (optional)
            </label>
            <input
              type="text"
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              placeholder="corporate, minimal, professional"
              className="w-full px-3 py-2 rounded-lg border border-border bg-background text-[13px] text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:ring-1 focus:ring-primary"
            />
            <p className="text-[10px] text-muted-foreground/50 mt-1">
              Comma-separated keywords
            </p>
          </div>

          {/* Thumbnail */}
          <div>
            <label className="text-[11px] text-muted-foreground/60 uppercase tracking-wide block mb-1.5">
              Thumbnail (optional)
            </label>
            <input
              ref={thumbInputRef}
              type="file"
              accept="image/*"
              onChange={handleThumbnailUpload}
              className="hidden"
            />
            <div className="flex items-center gap-2">
              <div className="w-16 h-12 rounded-md bg-secondary flex items-center justify-center overflow-hidden shrink-0">
                {thumbnail ? (
                  <img
                    src={`data:image/jpeg;base64,${thumbnail}`}
                    alt="Thumbnail"
                    className="w-full h-full object-cover"
                  />
                ) : (
                  <FileText className="w-5 h-5 text-muted-foreground/30" />
                )}
              </div>
              <button
                onClick={() => thumbInputRef.current?.click()}
                className="px-2.5 py-1.5 rounded-lg text-[11px] border border-border text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
              >
                {thumbnail ? "Change" : "Upload"}
              </button>
              {thumbnail && (
                <button
                  onClick={() => {
                    setThumbnail(null);
                    setThumbnailName("");
                  }}
                  className="px-2.5 py-1.5 rounded-lg text-[11px] text-muted-foreground hover:text-destructive transition-colors"
                >
                  Remove
                </button>
              )}
            </div>
          </div>

          {/* Validation */}
          <div className="space-y-2">
            <button
              onClick={handleValidate}
              disabled={
                validationState === "validating" || (mode === "add" && !file)
              }
              className={cn(
                "w-full flex items-center justify-center gap-2 px-3 py-2 rounded-lg text-[12px] font-medium transition-colors",
                validationState === "validating"
                  ? "bg-secondary text-muted-foreground cursor-wait"
                  : validationState === "valid"
                    ? "bg-emerald-500/10 text-emerald-400"
                    : validationState === "invalid"
                      ? "bg-red-500/10 text-red-400"
                      : "bg-secondary text-foreground hover:bg-accent",
              )}
            >
              {validationState === "validating" ? (
                <>
                  <Loader2 className="w-3.5 h-3.5 animate-spin" /> Validating...
                </>
              ) : validationState === "valid" ? (
                <>
                  <Check className="w-3.5 h-3.5" /> Validated
                </>
              ) : validationState === "invalid" ? (
                <>
                  <AlertCircle className="w-3.5 h-3.5" /> Validation Failed —
                  Retry
                </>
              ) : (
                <>
                  <Upload className="w-3.5 h-3.5" /> Test Template
                </>
              )}
            </button>

            {validationState === "valid" && validationDetails && (
              <p className="text-[10px] text-emerald-400/70 px-1">
                {validationDetails}
              </p>
            )}
            {validationState === "invalid" && validationError && (
              <div className="rounded-lg border border-red-500/20 bg-red-500/5 px-3 py-2">
                <p className="text-[11px] text-red-400">{validationError}</p>
                <p className="text-[10px] text-red-400/60 mt-1">
                  Fix the template and click "Test Template" to retry.
                </p>
              </div>
            )}
          </div>

          {/* Save button — only after successful validation */}
          {validationState === "valid" && (
            <button
              onClick={handleSave}
              disabled={isSaving || !displayName.trim()}
              className={cn(
                "w-full flex items-center justify-center gap-2 px-3 py-2 rounded-lg text-[12px] font-medium transition-colors",
                isSaving || !displayName.trim()
                  ? "bg-secondary text-muted-foreground/50 cursor-not-allowed"
                  : "bg-primary text-primary-foreground hover:bg-primary/90",
              )}
            >
              {isSaving ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <Check className="w-3.5 h-3.5" />
              )}
              {mode === "add" ? "Add Template" : "Save Changes"}
            </button>
          )}
        </div>
      </ScrollArea>
    </div>
  );
}
