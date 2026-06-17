import { useState, useEffect, useRef, useCallback } from "react";
import {
  Search,
  X,
  Plus,
  Pencil,
  Trash2,
  Pin,
  Loader2,
  Brain,
  Sparkles,
  Zap,
  User,
  Bot,
} from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useMemoryStore } from "@/store/memoryStore";
import { useT } from "@/store/settingsStore";
import { cn } from "@/lib/utils";
import type { MemoryItem } from "@/api/memoryClient";

// ─── Category Colors ────────────────────────────────────────────

const categoryColors: Record<
  string,
  { bg: string; text: string; border: string }
> = {
  identity: {
    bg: "bg-blue-500/10",
    text: "text-blue-500",
    border: "border-blue-500/20",
  },
  preference: {
    bg: "bg-purple-500/10",
    text: "text-purple-500",
    border: "border-purple-500/20",
  },
  fact: {
    bg: "bg-zinc-500/10",
    text: "text-zinc-400",
    border: "border-zinc-500/20",
  },
  contact: {
    bg: "bg-emerald-500/10",
    text: "text-emerald-500",
    border: "border-emerald-500/20",
  },
  project: {
    bg: "bg-amber-500/10",
    text: "text-amber-500",
    border: "border-amber-500/20",
  },
  goal: {
    bg: "bg-pink-500/10",
    text: "text-pink-500",
    border: "border-pink-500/20",
  },
};

const categoryList = [
  "identity",
  "preference",
  "fact",
  "contact",
  "project",
  "goal",
];

// ─── Relative time ──────────────────────────────────────────────

function relativeTime(timestamp: number): string {
  const now = Date.now();
  const diff = now - timestamp;
  const seconds = Math.floor(diff / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);

  if (days > 0) return `${days}d ago`;
  if (hours > 0) return `${hours}h ago`;
  if (minutes > 0) return `${minutes}m ago`;
  return "just now";
}

// ─── Source Icon ─────────────────────────────────────────────────

function SourceIcon({
  source,
  t,
}: {
  source: string;
  t: (key: string) => string;
}) {
  switch (source) {
    case "auto":
      return (
        <span
          className="flex items-center gap-1"
          title={t("brain.memories.source.auto")}
        >
          <Zap className="w-3 h-3" />
        </span>
      );
    case "user":
      return (
        <span
          className="flex items-center gap-1"
          title={t("brain.memories.source.user")}
        >
          <User className="w-3 h-3" />
        </span>
      );
    case "ai_agent":
      return (
        <span
          className="flex items-center gap-1"
          title={t("brain.memories.source.ai_agent")}
        >
          <Bot className="w-3 h-3" />
        </span>
      );
    default:
      return null;
  }
}

// ─── Category Badge ──────────────────────────────────────────────

function CategoryBadge({ category }: { category: string }) {
  const colors = categoryColors[category] ?? categoryColors.fact;
  return (
    <span
      className={cn(
        "inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium",
        colors.bg,
        colors.text,
      )}
    >
      {category}
    </span>
  );
}

// ─── Memory Card ─────────────────────────────────────────────────

function MemoryCard({
  memory,
  onEdit,
  onDelete,
  onPin,
  isEditing,
  editText,
  editCategory,
  onEditTextChange,
  onEditCategoryChange,
  onSaveEdit,
  onCancelEdit,
  t,
}: {
  memory: MemoryItem;
  onEdit: (id: string) => void;
  onDelete: (id: string) => void;
  onPin: (id: string, pinned: boolean) => void;
  isEditing: boolean;
  editText: string;
  editCategory: string;
  onEditTextChange: (text: string) => void;
  onEditCategoryChange: (category: string) => void;
  onSaveEdit: () => void;
  onCancelEdit: () => void;
  t: (key: string) => string;
}) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  const colors = categoryColors[memory.category] ?? categoryColors.fact;

  if (isEditing) {
    return (
      <div
        className={cn(
          "rounded-xl border p-4 transition-all",
          "border-border bg-card",
        )}
      >
        <div className="space-y-3">
          <textarea
            value={editText}
            onChange={(e) => onEditTextChange(e.target.value)}
            className="w-full min-h-[60px] px-3 py-2 rounded-lg border border-border bg-background text-[13px] text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:ring-1 focus:ring-primary resize-none"
            autoFocus
          />
          <div className="flex items-center gap-2">
            <select
              value={editCategory}
              onChange={(e) => onEditCategoryChange(e.target.value)}
              className="px-3 py-1.5 rounded-lg border border-border bg-background text-[12px] text-foreground focus:outline-none focus:ring-1 focus:ring-primary"
            >
              {categoryList.map((cat) => (
                <option key={cat} value={cat}>
                  {cat.charAt(0).toUpperCase() + cat.slice(1)}
                </option>
              ))}
            </select>
            <div className="flex-1" />
            <button
              onClick={onCancelEdit}
              className="px-3 py-1.5 rounded-lg text-[12px] text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
            >
              {t("brain.memories.cancel")}
            </button>
            <button
              onClick={onSaveEdit}
              className="px-3 py-1.5 rounded-lg text-[12px] bg-primary/10 text-primary hover:bg-primary/20 font-medium transition-colors"
            >
              {t("brain.memories.save")}
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div
      className={cn(
        "group rounded-xl border p-4 transition-all hover:bg-accent/30",
        memory.pinned
          ? "border-amber-500/30 bg-amber-500/5 border-l-2 border-l-amber-500"
          : "border-border bg-card",
      )}
    >
      <div className="flex items-start gap-3">
        {/* Content */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1.5">
            <CategoryBadge category={memory.category} />
            <SourceIcon source={memory.source} t={t} />
            <span className="text-[11px] text-muted-foreground/50">
              {relativeTime(memory.updated_at)}
            </span>
            {memory.uses > 0 && (
              <span className="text-[11px] text-muted-foreground/50">
                {t("brain.memories.used")} {memory.uses}{" "}
                {t("brain.memories.times")}
              </span>
            )}
          </div>
          <p className="text-[13px] text-foreground leading-relaxed">
            {memory.text}
          </p>
        </div>

        {/* Actions */}
        <div className="flex items-center gap-0.5 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
          <button
            onClick={() => onPin(memory.id, !memory.pinned)}
            className={cn(
              "w-7 h-7 rounded-lg flex items-center justify-center transition-colors",
              memory.pinned
                ? "text-amber-500 hover:bg-amber-500/10"
                : "text-muted-foreground/50 hover:text-foreground hover:bg-accent",
            )}
            title={
              memory.pinned
                ? t("brain.memories.unpin")
                : t("brain.memories.pin")
            }
          >
            <Pin className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={() => onEdit(memory.id)}
            className="w-7 h-7 rounded-lg flex items-center justify-center text-muted-foreground/50 hover:text-foreground hover:bg-accent transition-colors"
            title={t("brain.memories.edit")}
          >
            <Pencil className="w-3.5 h-3.5" />
          </button>
          {confirmDelete ? (
            <div className="flex items-center gap-1">
              <button
                onClick={() => onDelete(memory.id)}
                className="px-2 py-1 rounded-lg text-[11px] bg-destructive/10 text-destructive hover:bg-destructive/20 transition-colors font-medium"
              >
                {t("brain.memories.delete")}
              </button>
              <button
                onClick={() => setConfirmDelete(false)}
                className="px-2 py-1 rounded-lg text-[11px] text-muted-foreground hover:bg-accent transition-colors"
              >
                {t("brain.memories.cancel")}
              </button>
            </div>
          ) : (
            <button
              onClick={() => setConfirmDelete(true)}
              className="w-7 h-7 rounded-lg flex items-center justify-center text-muted-foreground/50 hover:text-destructive hover:bg-destructive/10 transition-colors"
              title={t("brain.memories.delete")}
            >
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Add Memory Form ─────────────────────────────────────────────

function AddMemoryForm({
  onSave,
  onCancel,
  t,
}: {
  onSave: (text: string, category: string) => void;
  onCancel: () => void;
  t: (key: string) => string;
}) {
  const [text, setText] = useState("");
  const [category, setCategory] = useState("fact");

  const handleSave = () => {
    if (!text.trim()) return;
    onSave(text.trim(), category);
  };

  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <div className="space-y-3">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={t("brain.memories.textPlaceholder")}
          className="w-full min-h-[60px] px-3 py-2 rounded-lg border border-border bg-background text-[13px] text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:ring-1 focus:ring-primary resize-none"
          autoFocus
        />
        <div className="flex items-center gap-2">
          <select
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className="px-3 py-1.5 rounded-lg border border-border bg-background text-[12px] text-foreground focus:outline-none focus:ring-1 focus:ring-primary"
          >
            {categoryList.map((cat) => (
              <option key={cat} value={cat}>
                {cat.charAt(0).toUpperCase() + cat.slice(1)}
              </option>
            ))}
          </select>
          <div className="flex-1" />
          <button
            onClick={onCancel}
            className="px-3 py-1.5 rounded-lg text-[12px] text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
          >
            {t("brain.memories.cancel")}
          </button>
          <button
            onClick={handleSave}
            disabled={!text.trim()}
            className={cn(
              "px-3 py-1.5 rounded-lg text-[12px] font-medium transition-colors",
              text.trim()
                ? "bg-primary/10 text-primary hover:bg-primary/20"
                : "bg-secondary text-muted-foreground/50 cursor-not-allowed",
            )}
          >
            {t("brain.memories.save")}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Memories Tab ────────────────────────────────────────────────

export function MemoriesTab() {
  const t = useT();

  const memories = useMemoryStore((s) => s.memories);
  const categories = useMemoryStore((s) => s.categories);
  const isLoading = useMemoryStore((s) => s.isLoading);
  const isAuditing = useMemoryStore((s) => s.isAuditing);
  const activeCategory = useMemoryStore((s) => s.activeCategory);
  const searchQuery = useMemoryStore((s) => s.searchQuery);
  const searchResults = useMemoryStore((s) => s.searchResults);
  const isSearching = useMemoryStore((s) => s.isSearching);
  const isExtracting = useMemoryStore((s) => s.isExtracting);

  const loadMemories = useMemoryStore((s) => s.loadMemories);
  const loadCategories = useMemoryStore((s) => s.loadCategories);
  const addMemory = useMemoryStore((s) => s.addMemory);
  const updateMemory = useMemoryStore((s) => s.updateMemory);
  const deleteMemory = useMemoryStore((s) => s.deleteMemory);
  const pinMemory = useMemoryStore((s) => s.pinMemory);
  const searchMemories = useMemoryStore((s) => s.searchMemories);
  const setActiveCategory = useMemoryStore((s) => s.setActiveCategory);
  const clearSearch = useMemoryStore((s) => s.clearSearch);
  const auditMemories = useMemoryStore((s) => s.auditMemories);

  const [showAddForm, setShowAddForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");
  const [editCategory, setEditCategory] = useState("fact");
  const [auditResult, setAuditResult] = useState<{
    before: number;
    after: number;
    removed: number;
  } | null>(null);

  const searchTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [localSearch, setLocalSearch] = useState("");

  // Load memories on mount
  useEffect(() => {
    loadMemories();
    loadCategories();
  }, [loadMemories, loadCategories]);

  // Reload memories when activeCategory changes
  useEffect(() => {
    loadMemories();
  }, [activeCategory, loadMemories]);

  // Debounced search
  const handleSearchChange = useCallback(
    (value: string) => {
      setLocalSearch(value);
      if (searchTimeoutRef.current) {
        clearTimeout(searchTimeoutRef.current);
      }
      if (!value.trim()) {
        clearSearch();
        return;
      }
      searchTimeoutRef.current = setTimeout(() => {
        searchMemories(value.trim());
      }, 300);
    },
    [searchMemories, clearSearch],
  );

  const handleClearSearch = () => {
    setLocalSearch("");
    clearSearch();
  };

  const handleAddMemory = async (text: string, category: string) => {
    await addMemory(text, category);
    setShowAddForm(false);
  };

  const handleStartEdit = (memory: MemoryItem) => {
    setEditingId(memory.id);
    setEditText(memory.text);
    setEditCategory(memory.category);
  };

  const handleSaveEdit = async () => {
    if (editingId && editText.trim()) {
      await updateMemory(editingId, editText.trim(), editCategory);
      setEditingId(null);
      setEditText("");
      setEditCategory("fact");
    }
  };

  const handleCancelEdit = () => {
    setEditingId(null);
    setEditText("");
    setEditCategory("fact");
  };

  const handleDelete = async (id: string) => {
    await deleteMemory(id);
  };

  const handlePin = async (id: string, pinned: boolean) => {
    await pinMemory(id, pinned);
  };

  const handleAudit = async () => {
    setAuditResult(null);
    await auditMemories();
    // Show audit result
    const beforeCount = memories.length;
    const afterMemories = useMemoryStore.getState().memories;
    const removed = beforeCount - afterMemories.length;
    setAuditResult({
      before: beforeCount,
      after: afterMemories.length,
      removed,
    });
    setTimeout(() => setAuditResult(null), 5000);
  };

  // Determine which memories to display
  const displayMemories = searchQuery.trim() ? searchResults : memories;

  // Sort: pinned first
  const sortedMemories = [...displayMemories].sort((a, b) => {
    if (a.pinned && !b.pinned) return -1;
    if (!a.pinned && b.pinned) return 1;
    return b.updated_at - a.updated_at;
  });

  // Get count for a category
  const getCategoryCount = (cat: string): number => {
    if (cat === "all") return memories.length;
    const found = categories.find((c) => c.category === cat);
    return found?.count ?? 0;
  };

  return (
    <div className="flex flex-col h-full">
      {/* Search & Controls */}
      <div className="px-4 pt-4 pb-3 space-y-3">
        {/* Search bar + Add/Audit buttons */}
        <div className="flex items-center gap-2">
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground/50" />
            <input
              type="text"
              value={localSearch}
              onChange={(e) => handleSearchChange(e.target.value)}
              placeholder={t("brain.memories.search")}
              className="w-full pl-9 pr-9 py-2 rounded-xl border border-border bg-card text-[13px] text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:ring-1 focus:ring-primary transition-colors"
            />
            {localSearch && (
              <button
                onClick={handleClearSearch}
                className="absolute right-2 top-1/2 -translate-y-1/2 w-5 h-5 rounded-full flex items-center justify-center text-muted-foreground/50 hover:text-foreground hover:bg-accent transition-colors"
              >
                <X className="w-3 h-3" />
              </button>
            )}
          </div>
          <button
            onClick={() => setShowAddForm(true)}
            className="shrink-0 flex items-center gap-1.5 px-3 py-2 rounded-xl text-[12px] font-medium bg-primary/10 text-primary hover:bg-primary/20 transition-colors"
          >
            <Plus className="w-3.5 h-3.5" />
            <span className="hidden sm:inline">{t("brain.memories.add")}</span>
          </button>
          <button
            onClick={handleAudit}
            disabled={isAuditing}
            className={cn(
              "shrink-0 flex items-center gap-1.5 px-3 py-2 rounded-xl text-[12px] font-medium transition-colors",
              isAuditing
                ? "bg-secondary text-muted-foreground/50 cursor-not-allowed"
                : "bg-secondary text-muted-foreground hover:bg-accent hover:text-foreground",
            )}
          >
            {isAuditing ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Sparkles className="w-3.5 h-3.5" />
            )}
            <span className="hidden sm:inline">
              {t("brain.memories.tidyUp")}
            </span>
          </button>
        </div>

        {/* Audit result */}
        {auditResult && auditResult.removed > 0 && (
          <div className="flex items-center gap-2 px-3 py-2 rounded-xl bg-emerald-500/10 text-emerald-500 text-[12px]">
            <Sparkles className="w-3.5 h-3.5" />
            {t("brain.memories.auditDone").replace(
              "{count}",
              String(auditResult.removed),
            )}
          </div>
        )}

        {/* Extracting indicator */}
        {isExtracting && (
          <div className="flex items-center gap-2 px-3 py-2 rounded-xl bg-primary/10 text-primary text-[12px]">
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
            {t("brain.memories.extracting")}
          </div>
        )}

        {/* Category filter pills */}
        <div className="flex gap-1.5 overflow-x-auto pb-1 -mx-1 px-1 scrollbar-none">
          <button
            onClick={() => setActiveCategory("all")}
            className={cn(
              "shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-full text-[12px] font-medium transition-colors",
              activeCategory === "all"
                ? "bg-primary/10 text-primary ring-1 ring-primary/20"
                : "bg-secondary text-muted-foreground hover:text-foreground",
            )}
          >
            {t("brain.memories.all")}
            <span className="text-[10px] opacity-60">
              {getCategoryCount("all")}
            </span>
          </button>
          {categoryList.map((cat) => {
            const colors = categoryColors[cat];
            const count = getCategoryCount(cat);
            return (
              <button
                key={cat}
                onClick={() => setActiveCategory(cat)}
                className={cn(
                  "shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-full text-[12px] font-medium transition-colors",
                  activeCategory === cat
                    ? `${colors.bg} ${colors.text} ring-1 ${colors.border}`
                    : "bg-secondary text-muted-foreground hover:text-foreground",
                )}
              >
                {cat.charAt(0).toUpperCase() + cat.slice(1)}
                {count > 0 && (
                  <span className="text-[10px] opacity-60">{count}</span>
                )}
              </button>
            );
          })}
        </div>
      </div>

      {/* Memory List */}
      <ScrollArea className="flex-1 px-4">
        <div className="space-y-2 pb-4">
          {/* Add memory form */}
          {showAddForm && (
            <AddMemoryForm
              onSave={handleAddMemory}
              onCancel={() => setShowAddForm(false)}
              t={t}
            />
          )}

          {/* Loading state */}
          {isLoading && (
            <div className="flex items-center justify-center py-12">
              <div className="flex flex-col items-center gap-3">
                <Loader2 className="w-6 h-6 text-primary animate-spin" />
                <p className="text-[12px] text-muted-foreground">Loading...</p>
              </div>
            </div>
          )}

          {/* Searching state */}
          {isSearching && !isLoading && (
            <div className="flex items-center justify-center py-8">
              <div className="flex items-center gap-2 text-muted-foreground">
                <Loader2 className="w-4 h-4 animate-spin" />
                <span className="text-[12px]">Searching...</span>
              </div>
            </div>
          )}

          {/* Empty state */}
          {!isLoading &&
            !isSearching &&
            sortedMemories.length === 0 &&
            !searchQuery.trim() && (
              <div className="flex flex-col items-center justify-center py-16 text-center">
                <Brain className="w-10 h-10 text-muted-foreground/20 mb-3" />
                <p className="text-[13px] text-muted-foreground/60 max-w-[260px]">
                  {t("brain.memories.empty")}
                </p>
              </div>
            )}

          {/* Empty search results */}
          {!isLoading &&
            !isSearching &&
            sortedMemories.length === 0 &&
            searchQuery.trim() && (
              <div className="flex flex-col items-center justify-center py-16 text-center">
                <Search className="w-8 h-8 text-muted-foreground/20 mb-3" />
                <p className="text-[13px] text-muted-foreground/60">
                  {t("brain.memories.emptySearch")}
                </p>
              </div>
            )}

          {/* Memory cards */}
          {!isLoading &&
            !isSearching &&
            sortedMemories.map((memory) => (
              <MemoryCard
                key={memory.id}
                memory={memory}
                onEdit={(id) => handleStartEdit(memory)}
                onDelete={handleDelete}
                onPin={handlePin}
                isEditing={editingId === memory.id}
                editText={editText}
                editCategory={editCategory}
                onEditTextChange={setEditText}
                onEditCategoryChange={setEditCategory}
                onSaveEdit={handleSaveEdit}
                onCancelEdit={handleCancelEdit}
                t={t}
              />
            ))}
        </div>
      </ScrollArea>
    </div>
  );
}
