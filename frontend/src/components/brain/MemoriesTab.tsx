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
  MoreHorizontal,
} from "lucide-react";
import { useMemoryStore } from "@/store/memoryStore";
import { useT } from "@/store/settingsStore";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { EmptyState, FilterChip } from "@/components/ui/primitives";
import type { MemoryItem } from "@/api/memoryClient";

// ─── Category Colors ────────────────────────────────────────────

const categoryColors: Record<string, { bg: string; text: string }> = {
  identity: {
    bg: "bg-blue-500/10",
    text: "text-blue-600 dark:text-blue-400",
  },
  preference: {
    bg: "bg-purple-500/10",
    text: "text-purple-600 dark:text-purple-400",
  },
  fact: {
    bg: "bg-secondary",
    text: "text-muted-foreground",
  },
  contact: {
    bg: "bg-emerald-500/10",
    text: "text-emerald-600 dark:text-emerald-400",
  },
  project: {
    bg: "bg-amber-500/10",
    text: "text-amber-700 dark:text-amber-400",
  },
  goal: {
    bg: "bg-pink-500/10",
    text: "text-pink-600 dark:text-pink-400",
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

const categoryLabel = (cat: string) =>
  cat.charAt(0).toUpperCase() + cat.slice(1);

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
          <Zap className="w-3 h-3 text-muted-foreground/80" />
        </span>
      );
    case "user":
      return (
        <span
          className="flex items-center gap-1"
          title={t("brain.memories.source.user")}
        >
          <User className="w-3 h-3 text-muted-foreground/80" />
        </span>
      );
    case "ai_agent":
      return (
        <span
          className="flex items-center gap-1"
          title={t("brain.memories.source.ai_agent")}
        >
          <Bot className="w-3 h-3 text-muted-foreground/80" />
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
        "inline-flex h-5 shrink-0 items-center rounded-md px-1.5 text-[11px] font-medium",
        colors.bg,
        colors.text,
      )}
    >
      {categoryLabel(category)}
    </span>
  );
}

// ─── Memory Row ──────────────────────────────────────────────────

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

  if (isEditing) {
    return (
      <div className="rounded-xl bg-secondary/60 p-4">
        <div className="space-y-3">
          <textarea
            value={editText}
            onChange={(e) => onEditTextChange(e.target.value)}
            className="w-full min-h-16 px-3 py-2 rounded-lg border border-border/60 bg-background text-[13.5px] text-foreground leading-relaxed placeholder:text-muted-foreground/70 focus:outline-none focus:border-primary/50 focus:ring-2 focus:ring-primary/20 transition-colors resize-none"
            autoFocus
          />
          <div className="flex items-center gap-2">
            <select
              value={editCategory}
              onChange={(e) => onEditCategoryChange(e.target.value)}
              className="h-8 px-2.5 rounded-lg border border-border/60 bg-background text-[12.5px] text-foreground focus:outline-none focus:border-primary/50 focus:ring-2 focus:ring-primary/20"
            >
              {categoryList.map((cat) => (
                <option key={cat} value={cat}>
                  {categoryLabel(cat)}
                </option>
              ))}
            </select>
            <div className="flex-1" />
            <Button variant="ghost" size="xs" onClick={onCancelEdit}>
              {t("brain.memories.cancel")}
            </Button>
            <Button variant="default" size="xs" onClick={onSaveEdit}>
              {t("brain.memories.save")}
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="group flex items-start gap-3 rounded-lg py-3 pl-2 pr-1 transition-colors hover:bg-surface-hover">
      {/* Content */}
      <div className="flex-1 min-w-0">
        <p className="text-[13.5px] text-foreground leading-relaxed wrap-break-word">
          {memory.pinned && (
            <Pin
              className="mr-1.5 -mt-0.5 inline h-3 w-3 text-warning"
              aria-label={t("brain.memories.unpin")}
            />
          )}
          {memory.text}
        </p>
        <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
          <CategoryBadge category={memory.category} />
          <SourceIcon source={memory.source} t={t} />
          <span>{relativeTime(memory.updated_at)}</span>
          {memory.uses > 0 && (
            <span className="hidden sm:inline">
              {t("brain.memories.used")} {memory.uses}{" "}
              {t("brain.memories.times")}
            </span>
          )}
        </div>
      </div>

      {/* Actions */}
      <div className="flex shrink-0 items-center gap-0.5 pt-0.5 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
        {confirmDelete ? (
          <div className="flex items-center gap-1">
            <span className="mr-1 hidden text-xs text-muted-foreground sm:inline">
              {t("brain.memories.deleteConfirm")}
            </span>
            <Button
              variant="ghost"
              size="xs"
              onClick={() => setConfirmDelete(false)}
            >
              {t("brain.memories.cancel")}
            </Button>
            <Button
              variant="destructive"
              size="xs"
              onClick={() => onDelete(memory.id)}
            >
              {t("brain.memories.delete")}
            </Button>
          </div>
        ) : (
          <DropdownMenu>
            <DropdownMenuTrigger
              aria-label={t("brain.memories.actions")}
              className="flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60 data-[state=open]:bg-secondary data-[state=open]:text-foreground"
            >
              <MoreHorizontal className="h-4 w-4" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                onClick={() => onPin(memory.id, !memory.pinned)}
              >
                <Pin />
                {memory.pinned
                  ? t("brain.memories.unpin")
                  : t("brain.memories.pin")}
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => onEdit(memory.id)}>
                <Pencil />
                {t("brain.memories.edit")}
              </DropdownMenuItem>
              <DropdownMenuItem
                className="text-danger focus:text-danger [&_svg]:text-danger"
                onClick={() => setConfirmDelete(true)}
              >
                <Trash2 />
                {t("brain.memories.delete")}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
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
    <div className="rounded-xl bg-secondary/60 p-4">
      <div className="space-y-3">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={t("brain.memories.textPlaceholder")}
          className="w-full min-h-16 px-3 py-2 rounded-lg border border-border/60 bg-background text-[13.5px] text-foreground leading-relaxed placeholder:text-muted-foreground/70 focus:outline-none focus:border-primary/50 focus:ring-2 focus:ring-primary/20 transition-colors resize-none"
          autoFocus
        />
        <div className="flex items-center gap-2">
          <select
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className="h-8 px-2.5 rounded-lg border border-border/60 bg-background text-[12.5px] text-foreground focus:outline-none focus:border-primary/50 focus:ring-2 focus:ring-primary/20"
          >
            {categoryList.map((cat) => (
              <option key={cat} value={cat}>
                {categoryLabel(cat)}
              </option>
            ))}
          </select>
          <div className="flex-1" />
          <Button variant="ghost" size="xs" onClick={onCancel}>
            {t("brain.memories.cancel")}
          </Button>
          <Button
            variant="default"
            size="xs"
            onClick={handleSave}
            disabled={!text.trim()}
          >
            {t("brain.memories.save")}
          </Button>
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
    <div className="flex flex-col">
      {/* Search & Controls */}
      <div className="space-y-3 pb-5">
        {/* Search bar + Add/Audit buttons */}
        <div className="flex items-center gap-2">
          <div className="relative w-full sm:max-w-sm">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground/80" />
            <input
              type="text"
              value={localSearch}
              onChange={(e) => handleSearchChange(e.target.value)}
              placeholder={t("brain.memories.search")}
              className="h-9 w-full rounded-lg border border-border/60 bg-transparent pl-9 pr-8 text-[13px] text-foreground placeholder:text-muted-foreground/80 transition-colors focus:border-primary/50 focus:outline-none focus:ring-2 focus:ring-primary/20"
            />
            {localSearch && (
              <button
                onClick={handleClearSearch}
                aria-label={t("brain.memories.search")}
                className="absolute right-2 top-1/2 flex h-5 w-5 -translate-y-1/2 items-center justify-center rounded-full text-muted-foreground/80 transition-colors hover:bg-surface-hover hover:text-foreground"
              >
                <X className="h-3 w-3" />
              </button>
            )}
          </div>
          <div className="flex-1" />
          <Button
            variant="secondary"
            size="sm"
            onClick={handleAudit}
            disabled={isAuditing}
          >
            {isAuditing ? <Loader2 className="animate-spin" /> : <Sparkles />}
            <span className="hidden sm:inline">
              {t("brain.memories.tidyUp")}
            </span>
          </Button>
          <Button
            variant="default"
            size="sm"
            onClick={() => setShowAddForm(true)}
          >
            <Plus />
            <span className="hidden sm:inline">{t("brain.memories.add")}</span>
          </Button>
        </div>

        {/* Audit result */}
        {auditResult && auditResult.removed > 0 && (
          <div className="flex items-center gap-2 rounded-lg bg-success/10 px-3 py-2 text-[12.5px] text-success">
            <Sparkles className="h-3.5 w-3.5" />
            {t("brain.memories.auditDone").replace(
              "{count}",
              String(auditResult.removed),
            )}
          </div>
        )}

        {/* Extracting indicator */}
        {isExtracting && (
          <div className="flex items-center gap-2 rounded-lg bg-primary/10 px-3 py-2 text-[12.5px] text-primary">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            {t("brain.memories.extracting")}
          </div>
        )}

        {/* Category filter chips */}
        <div className="-mx-1 flex gap-1 overflow-x-auto px-1 pb-0.5">
          <FilterChip
            active={activeCategory === "all"}
            onClick={() => setActiveCategory("all")}
          >
            {t("brain.memories.all")}
            <span className="ml-1 tabular-nums opacity-60">
              {getCategoryCount("all")}
            </span>
          </FilterChip>
          {categoryList.map((cat) => {
            const count = getCategoryCount(cat);
            return (
              <FilterChip
                key={cat}
                active={activeCategory === cat}
                onClick={() => setActiveCategory(cat)}
              >
                {categoryLabel(cat)}
                {count > 0 && (
                  <span className="ml-1 tabular-nums opacity-60">{count}</span>
                )}
              </FilterChip>
            );
          })}
        </div>
      </div>

      {/* Memory List */}
      <div className="divide-y divide-border/50">
        {/* Add memory form */}
        {showAddForm && (
          <div className="pb-4">
            <AddMemoryForm
              onSave={handleAddMemory}
              onCancel={() => setShowAddForm(false)}
              t={t}
            />
          </div>
        )}

        {/* Loading state */}
        {isLoading && (
          <div className="flex items-center justify-center py-12">
            <div className="flex flex-col items-center gap-3">
              <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
              <p className="text-xs text-muted-foreground">Loading...</p>
            </div>
          </div>
        )}

        {/* Searching state */}
        {isSearching && !isLoading && (
          <div className="flex items-center justify-center py-8">
            <div className="flex items-center gap-2 text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              <span className="text-xs">Searching...</span>
            </div>
          </div>
        )}

        {/* Empty state */}
        {!isLoading &&
          !isSearching &&
          sortedMemories.length === 0 &&
          !searchQuery.trim() && (
            <EmptyState
              icon={<Brain />}
              title={t("brain.memories.emptyTitle")}
              description={t("brain.memories.empty")}
            />
          )}

        {/* Empty search results */}
        {!isLoading &&
          !isSearching &&
          sortedMemories.length === 0 &&
          searchQuery.trim() && (
            <EmptyState
              icon={<Search />}
              title={t("brain.memories.emptySearch")}
            />
          )}

        {/* Memory rows */}
        {!isLoading &&
          !isSearching &&
          sortedMemories.map((memory) => (
            <MemoryCard
              key={memory.id}
              memory={memory}
              onEdit={(_id: string) => handleStartEdit(memory)}
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
    </div>
  );
}
