import json
import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional, Set

import yaml
from pydantic import PrivateAttr
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

# Valid profile names — profiles outside this set are rejected
VALID_PROFILES = {
    "cpu_small",
    "cpu_medium",
    "nvidia_small",
    "nvidia_medium",
    "nvidia_large",
    "nvidia_xlarge",
    "apple_small",
    "apple_medium",
    "apple_large",
    "apple_xlarge",
}


# ─── Model Config ────────────────────────────────────────────────────


class ModelConfig:
    """A single model entry from profiles.yml or modules.yml."""

    def __init__(self, data: dict):
        self.id: str = data["id"]
        self.type: str = data.get("type", "chat")
        self.role: str = data.get("role", "")
        self.description: str = data.get("description", "")
        self.size: str = data.get("size", "")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "role": self.role,
            "description": self.description,
            "size": self.size,
        }


# ─── Profile Config ──────────────────────────────────────────────────


class ProfileConfig:
    """A hardware profile with its list of models."""

    def __init__(self, name: str, data: dict):
        self.name = name
        self.description: str = data.get("description", "")
        self.label: str = data.get("label", name)
        self.engine: str = data.get("engine", "ollama")
        self.models: List[ModelConfig] = [
            ModelConfig(m) for m in data.get("models", [])
        ]

    def get_model_by_role(self, role: str) -> Optional[ModelConfig]:
        """Find a model by its role (e.g. 'default', 'default_vision')."""
        for m in self.models:
            if m.role == role:
                return m
        return None

    def get_models_by_type(self, model_type: str) -> List[ModelConfig]:
        """Find all models of a given type (e.g. 'chat', 'vision')."""
        return [m for m in self.models if m.type == model_type]

    def get_default_model(self) -> str:
        """Return the default chat model ID for this profile."""
        m = self.get_model_by_role("default")
        return m.id if m else "qwen3:4b"


def load_profiles(profiles_path: Optional[str] = None) -> Dict[str, ProfileConfig]:
    """Load all profiles from profiles.yml."""
    if profiles_path is None:
        # Look for profiles.yml in the project root (2 levels up from this file)
        # app/config.py -> app/ -> backend/ -> project_root/
        # But in Docker, profiles.yml is copied to /app/profiles.yml
        candidates = [
            Path("/app/profiles.yml"),  # Inside Docker container
            Path(__file__).parent.parent.parent
            / "profiles.yml",  # Dev: backend/../profiles.yml
        ]
        for p in candidates:
            if p.exists():
                profiles_path = str(p)
                break

    if profiles_path is None or not Path(profiles_path).exists():
        # Fallback: return a minimal default
        logger.warning("profiles.yml not found — using fallback cpu_small profile")
        return {
            "cpu_small": ProfileConfig(
                "cpu_small",
                {
                    "description": "Fallback profile — profiles.yml not found",
                    "label": "CPU · Small (fallback)",
                    "engine": "ollama",
                    "models": [
                        {
                            "id": "qwen3:4b",
                            "type": "chat",
                            "role": "default",
                            "description": "Fallback model — profiles.yml not found",
                        }
                    ],
                },
            )
        }

    with open(profiles_path, "r") as f:
        data = yaml.safe_load(f)

    profiles = {}
    for name, pdata in data.get("profiles", {}).items():
        profiles[name] = ProfileConfig(name, pdata)

    return profiles


# ─── Module Config ───────────────────────────────────────────────────


class ModuleConfig:
    """A module definition from modules.yml.

    Required modules (like 'assistant') are always enabled and their models
    come from profiles.yml (referenced by role).

    Optional modules (like 'image_generation') can be toggled at runtime.
    Their models are defined per-profile in modules.yml itself — profiles
    not listed mean the module is unavailable on that hardware.
    """

    def __init__(self, name: str, data: dict):
        self.name = name
        self.required: bool = data.get("required", False)
        self.label: str = data.get("label", name)
        self.description: str = data.get("description", "")
        self.icon: str = data.get("icon", "puzzle")
        self.tools: List[str] = data.get("tools", [])
        self.minimum_requirements: Optional[dict] = (
            data.get("minimum_requirements") or None
        )
        self.estimated_size: str = data.get("estimated_size", "")

        # Store raw models data for deferred resolution
        self._models_data = data.get("models", {})

    def is_required(self) -> bool:
        return self.required

    def is_available_for_profile(self, profile_name: str) -> bool:
        """Check if this module has models for the given hardware profile.

        Required modules are always available (their models come from profiles.yml).
        Optional modules are available only if the profile is listed in their
        models dict with a non-null value.
        """
        if self.required:
            return True

        # Optional module: models dict has profile names as keys
        if not self._models_data:
            return False

        # If models is a list, it's a required module's role list
        if isinstance(self._models_data, list):
            return True

        # If models is a dict, check for the profile
        if isinstance(self._models_data, dict):
            return (
                profile_name in self._models_data
                and self._models_data[profile_name] is not None
            )

        return False

    def get_models_for_profile(self, profile_name: str) -> List[ModelConfig]:
        """Get models for this module for a specific profile.

        For required modules: returns [] (models come from profiles.yml).
        For optional modules: returns the model list for the profile, or [].
        """
        if self.required:
            return []

        if not self._models_data:
            return []

        # If it's a list, it's role references (required module style)
        if isinstance(self._models_data, list):
            return []

        # If it's a dict, look up by profile name
        if isinstance(self._models_data, dict):
            profile_models = self._models_data.get(profile_name)
            if profile_models is None:
                return []
            return [ModelConfig(m) for m in profile_models]

        return []

    def get_model_roles(self) -> List[str]:
        """For required modules: return the list of model roles used."""
        if isinstance(self._models_data, list):
            return [m.get("role", "") for m in self._models_data if m.get("role")]
        return []

    def get_all_profile_models(self) -> Dict[str, List[ModelConfig]]:
        """Get all models for all profiles (for optional modules).

        Returns dict of profile_name → [ModelConfig].
        """
        if not isinstance(self._models_data, dict) or self.required:
            return {}

        result = {}
        for profile_name, model_list in self._models_data.items():
            if model_list is not None:
                result[profile_name] = [ModelConfig(m) for m in model_list]
        return result

    def meets_requirements(self, ram_gb: int, vram_gb: int) -> bool:
        """Check if the given hardware meets this module's minimum requirements."""
        if not self.minimum_requirements:
            return True

        min_ram = self.minimum_requirements.get("ram", 0)
        min_vram = self.minimum_requirements.get("vram", 0)

        return ram_gb >= min_ram and vram_gb >= min_vram

    @staticmethod
    async def check_model_downloaded(model_id: str) -> bool:
        """Check if a model is available in Ollama.

        Shared utility to avoid duplication between modules API and tools.
        """
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{settings.OLLAMA_BASE_URL}/api/tags")
                if response.status_code == 200:
                    data = response.json()
                    available = [m.get("name", "") for m in data.get("models", [])]
                    model_base = model_id.split(":")[0]
                    return model_id in available or any(
                        m.split(":")[0] == model_base for m in available
                    )
        except Exception:
            pass
        return False

    def to_dict(
        self, profile_name: str, enabled: bool, models_downloaded: bool = False
    ) -> dict:
        """Serialize module info for API responses."""
        result = {
            "name": self.name,
            "required": self.required,
            "enabled": enabled,
            "label": self.label,
            "description": self.description,
            "icon": self.icon,
            "available": self.is_available_for_profile(profile_name),
            "models_downloaded": models_downloaded,
        }

        if not self.required:
            if self.minimum_requirements:
                result["minimum_requirements"] = self.minimum_requirements
            result["estimated_size"] = self.estimated_size
            result["models"] = [
                m.to_dict() for m in self.get_models_for_profile(profile_name)
            ]
        else:
            # For required modules, list the model roles
            result["model_roles"] = self.get_model_roles()

        return result


def load_modules(modules_path: Optional[str] = None) -> Dict[str, ModuleConfig]:
    """Load all modules from modules.yml."""
    if modules_path is None:
        candidates = [
            Path("/app/modules.yml"),  # Inside Docker container
            Path(__file__).parent.parent.parent
            / "modules.yml",  # Dev: backend/../modules.yml
        ]
        for p in candidates:
            if p.exists():
                modules_path = str(p)
                break

    if modules_path is None or not Path(modules_path).exists():
        logger.warning("modules.yml not found — using fallback assistant-only module")
        return {
            "assistant": ModuleConfig(
                "assistant",
                {
                    "required": True,
                    "label": "AI Assistant",
                    "description": "Core AI assistant (fallback — modules.yml not found)",
                    "icon": "bot",
                    "tools": [
                        "use_websearch",
                        "use_webfetch",
                        "use_code_exec",
                        "use_vision",
                    ],
                    "models": [{"role": "default"}],
                },
            )
        }

    try:
        with open(modules_path, "r") as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, OSError) as e:
        logger.error("Failed to parse modules.yml: %s — using fallback", e)
        return {
            "assistant": ModuleConfig(
                "assistant",
                {
                    "required": True,
                    "label": "AI Assistant",
                    "description": "Core AI assistant (fallback — modules.yml parse error)",
                    "icon": "bot",
                    "tools": [
                        "use_websearch",
                        "use_webfetch",
                        "use_code_exec",
                        "use_vision",
                    ],
                    "models": [{"role": "default"}],
                },
            )
        }

    modules = {}
    for name, mdata in data.get("modules", {}).items():
        try:
            modules[name] = ModuleConfig(name, mdata)
        except Exception as e:
            logger.error("Failed to load module '%s': %s", name, e)

    if not modules:
        logger.warning("No modules loaded from modules.yml — using fallback")

    return modules


# ─── Settings ────────────────────────────────────────────────────────


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Application
    APP_NAME: str = "RealOpen-AI"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://realopen:realopen@postgres:5432/realopen"

    # Ollama (runs natively on host)
    OLLAMA_BASE_URL: str = "http://host.docker.internal:11434"

    # SearxNG
    SEARXNG_BASE_URL: str = "http://searxng:8080"

    # Redis
    REDIS_URL: str = "redis://redis:6379/0"

    # CORS
    CORS_ORIGINS: List[str] = [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://localhost",
    ]

    # Hardware Profile (set by setup.sh from profiles.yml)
    # Must be one of the valid profile names — invalid values are rejected
    HARDWARE_PROFILE: str = "cpu_small"
    DEFAULT_MODEL: str = (
        "qwen3:4b"  # Fallback only; resolved from profiles.yml at runtime
    )

    # Modules — comma-separated list of enabled optional module names.
    # Required modules are always enabled regardless of this setting.
    ENABLED_MODULES: str = "assistant"

    # Setup mode — when true, the app shows the setup wizard on first run.
    # This is auto-detected at startup if data/.setup-complete marker doesn't exist.
    SETUP_MODE: bool = False

    # Ngrok
    NGROK_ENABLED: bool = False
    NGROK_AUTHTOKEN: str = ""

    # ── Memory system ────────────────────────────────────────────────────
    # These all have sensible defaults; override via env vars if needed.
    # Embedding model role (resolved via profiles.yml -> default_embedding).
    # nomic-embed-text:v1.5 produces 768-dim vectors — matches the
    # `memories.embedding` column after migration a8f3c2e1b7d4.
    MEMORY_EMBEDDING_MODEL_ROLE: str = "default_embedding"
    # Extraction model role — defaults to the chat model. Users can swap to
    # a smaller utility model by adding a `default_utility` role to
    # profiles.yml; until then, resolve_model() falls through to the
    # `default` chat model.
    MEMORY_EXTRACTION_MODEL_ROLE: str = "default_utility"
    # Audit model role — same as extraction by default.
    MEMORY_AUDIT_MODEL_ROLE: str = "default_utility"
    # Trigger extraction when there are >= this many NEW messages since the
    # last extraction (watermark-based, conversation-scoped).
    MEMORY_EXTRACTION_INTERVAL: int = 4
    # How many new memories to add before auto-triggering an audit.
    MEMORY_AUDIT_INTERVAL: int = 5
    # Cosine similarity threshold for vector dedup at extraction time.
    MEMORY_DEDUP_VECTOR_THRESHOLD: float = 0.85
    # Stricter threshold applied when BOTH texts are very short (<5 content
    # tokens after stop-word removal). Short texts are noisier in embedding
    # space — at 5 tokens, even paraphrases need to be near-identical.
    MEMORY_DEDUP_SHORT_TEXT_THRESHOLD: float = 0.92
    # Min Jaccard overlap on CONTENT tokens (after stop-word removal) for
    # a vector match to be accepted as a true duplicate. This is the
    # content-aware guard that prevents the Clémence-vs-Abdel false
    # positive: even if embeddings say "similar", if the two texts share
    # almost no content tokens, they aren't the same fact.
    MEMORY_DEDUP_CONTENT_MIN_OVERLAP: float = 0.10
    # Jaccard threshold for the text-fallback dedup tier.
    MEMORY_DEDUP_TEXT_THRESHOLD: float = 0.6
    # Hybrid retrieval weights (vector + BM25 + recency)
    MEMORY_RETRIEVAL_VECTOR_WEIGHT: float = 0.55
    MEMORY_RETRIEVAL_BM25_WEIGHT: float = 0.40
    MEMORY_RETRIEVAL_RECENCY_WEIGHT: float = 0.05
    # Gate: drop a memory if BOTH vector_sim < this AND bm25_norm < this.
    MEMORY_RETRIEVAL_GATE_VECTOR: float = 0.20
    MEMORY_RETRIEVAL_GATE_BM25: float = 0.08
    # Final cutoff: only keep memories with final score > this.
    MEMORY_RETRIEVAL_CUTOFF: float = 0.12
    # How many memories to inject into the system prompt (top-k after hybrid retrieval).
    MEMORY_INJECTION_TOP_K: int = 5
    # Context window (in messages) sent to the extraction LLM.
    MEMORY_EXTRACTION_CONTEXT_WINDOW: int = 6

    # ── Context compaction ──────────────────────────────────────────────
    # When context usage exceeds this fraction of the model's window,
    # compact older messages into a summary. Default 0.80 = 80% full.
    CONTEXT_COMPACT_THRESHOLD: float = 0.80
    # Number of recent turns to always preserve during compaction.
    CONTEXT_COMPACT_PRESERVE_TURNS: int = 6
    # Maximum tokens for the compaction summary response.
    CONTEXT_COMPACT_SUMMARY_TOKENS: int = 512

    # ── Conversation memory (cross-session context) ─────────────────────
    # Minimum messages before a conversation is summarized for cross-session memory.
    CONVERSATION_SUMMARY_MIN_MESSAGES: int = 8
    # Maximum past conversation summaries to inject into new conversations.
    CONVERSATION_SUMMARY_MAX_INJECT: int = 2
    # Minimum time between re-summaries of the same conversation (seconds).
    CONVERSATION_SUMMARY_COOLDOWN_SECONDS: int = 3600

    # ── RAG (document retrieval) ────────────────────────────────────────
    # Embedding + vision model roles (resolved via profiles.yml).
    RAG_EMBEDDING_MODEL_ROLE: str = "default_embedding"
    RAG_VISION_MODEL_ROLE: str = "default_vision"
    # Directory (under data/) where raw uploaded files are stored.
    # Each document gets a subdirectory named after its UUID.
    RAG_DOCUMENTS_DIR: str = "documents"

    # Adaptive chunking — chunk size depends on total document length so
    # short docs get fine-grained chunks (better Q&A precision) while
    # long docs get bigger chunks (less context fragmentation).
    #   <5k chars  → small  (RAG_CHUNK_SIZE_SMALL)
    #   5k–50k     → medium (RAG_CHUNK_SIZE_MEDIUM)
    #   >50k       → large  (RAG_CHUNK_SIZE_LARGE)
    RAG_CHUNK_SIZE_SMALL: int = 500
    RAG_CHUNK_SIZE_MEDIUM: int = 1000
    RAG_CHUNK_SIZE_LARGE: int = 2000
    # Overlap as a fraction of chunk size. 0.2 = 20% overlap.
    RAG_CHUNK_OVERLAP_RATIO: float = 0.2
    # Char-count thresholds for choosing small/medium/large chunk size.
    RAG_SMALL_DOC_THRESHOLD: int = 5000
    RAG_LARGE_DOC_THRESHOLD: int = 50000

    # Retrieval — per-doc adaptive.
    # Take top RAG_TOP_K_PER_DOC chunks per matched document, then keep the
    # top RAG_TOP_K_TOTAL overall. This guarantees a single big document
    # can't crowd out hits from other relevant docs.
    RAG_TOP_K_PER_DOC: int = 3
    RAG_TOP_K_TOTAL: int = 8
    # Drop chunks whose vector cosine similarity is below this.
    RAG_SIMILARITY_CUTOFF: float = 0.20
    # Hybrid retrieval weights (vector + BM25). Recency is irrelevant for
    # documents so it's omitted (unlike the memory system).
    RAG_RETRIEVAL_VECTOR_WEIGHT: float = 0.65
    RAG_RETRIEVAL_BM25_WEIGHT: float = 0.35

    # Max image size (pixels per side) sent to the vision model. Larger
    # images are downscaled to keep vision-LLM latency reasonable.
    RAG_VISION_IMAGE_MAX_DIM: int = 1024

    # ── RAG quality enhancements ────────────────────────────────────────
    # When true, rag_search expands the user's query with keyword synonyms
    # before retrieval (cheap, no LLM call). Improves recall for short
    # queries on small models that can't reformulate themselves.
    RAG_QUERY_EXPANSION: bool = True
    # When true, rag_search runs a lightweight LLM reranker over the top-K
    # retrieved chunks before returning them to the agent. Costs one extra
    # small-LLM call per rag_search invocation but meaningfully reorders
    # results. Uses the default_utility model (falls back to chat model).
    RAG_LLM_RERANK: bool = True
    # How many chunks to send to the reranker (top-N after hybrid retrieval).
    RAG_RERANK_TOP_N: int = 8
    # How many chunks to return to the agent after reranking.
    RAG_RERANK_FINAL_K: int = 5
    # Candidate pool size for BM25 scoring. The retrieval pipeline fetches
    # this many vector candidates, THEN scores them with BM25 (previously
    # only 30 vector candidates were BM25-scored, biasing toward vector
    # similarity and hiding keyword-only matches).
    RAG_CANDIDATE_POOL: int = 60

    # ── Memory + cross-session improvements ─────────────────────────────
    # When true, injected memory/RAG/cross-session blocks are wrapped in
    # untrusted-context guard markers (prompt-injection defense).
    MEMORY_UNTRUSTED_WRAP: bool = True
    # When true, memory extraction + audit run as a sequential background
    # queue AFTER the chat stream goes idle (protects the chat model's KV
    # cache on local 4-slot backends like llama.cpp). When false, they run
    # inline (old behavior — blocks the SSE stream).
    MEMORY_BACKGROUND_QUEUE: bool = True
    # Max wait (seconds) for the chat stream to go idle before the background
    # queue gives up and runs anyway.
    MEMORY_BG_QUEUE_MAX_WAIT: int = 120
    # Poll interval (seconds) for the background queue's idle check.
    MEMORY_BG_QUEUE_POLL: float = 0.25

    # ── KV-cache-aware system prompt ────────────────────────────────────
    # When true, the agent builds a STABLE system prefix (agent persona +
    # tool list + untrusted-context policy) and appends DYNAMIC content
    # (memories, RAG, cross-session, current datetime) as tail user-role
    # context messages. This lets Ollama/llama.cpp reuse their cached
    # prompt prefix across turns, halving per-turn latency on small models.
    KV_CACHE_AWARE_PROMPT: bool = True

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    # Private attributes — use PrivateAttr so Pydantic won't try to
    # validate / deepcopy them (threading.Lock is not picklable).
    _profiles: Optional[Dict[str, ProfileConfig]] = PrivateAttr(default=None)
    _modules: Optional[Dict[str, ModuleConfig]] = PrivateAttr(default=None)
    _enabled_modules_set: Optional[Set[str]] = PrivateAttr(default=None)
    _toggle_lock: threading.Lock = PrivateAttr()

    def model_post_init(self, __context) -> None:
        """Initialize non-picklable private attributes after Pydantic init."""
        self._toggle_lock = threading.Lock()

    def get_profiles(self) -> Dict[str, ProfileConfig]:
        """Load profiles.yml once and cache it."""
        if self._profiles is None:
            self._profiles = load_profiles()
        return self._profiles

    def get_current_profile(self) -> ProfileConfig:
        """Get the ProfileConfig for the current HARDWARE_PROFILE.

        If the profile name is invalid (not in VALID_PROFILES), this is a
        configuration error — likely a stale .env from an older version.
        Logs a warning and falls back to cpu_small.
        """
        profiles = self.get_profiles()

        # Reject invalid profile names (e.g. old "8gb", "16gb" names)
        if self.HARDWARE_PROFILE not in VALID_PROFILES:
            logger.warning(
                "Invalid HARDWARE_PROFILE='%s' — must be one of: %s. "
                "Falling back to 'cpu_small'. "
                "Delete your .env file and rerun setup.sh to fix this.",
                self.HARDWARE_PROFILE,
                ", ".join(sorted(VALID_PROFILES)),
            )
            self.HARDWARE_PROFILE = "cpu_small"

        return profiles.get(self.HARDWARE_PROFILE, profiles.get("cpu_small"))

    def get_modules(self) -> Dict[str, ModuleConfig]:
        """Load modules.yml once and cache it."""
        if self._modules is None:
            self._modules = load_modules()
        return self._modules

    def get_enabled_module_names(self) -> Set[str]:
        """Parse and cache the ENABLED_MODULES setting.

        Resolution order:
        1. Check for persisted state file (from a previous runtime toggle)
        2. Fall back to ENABLED_MODULES env var
        3. Always include required modules
        4. Filter out unknown module names
        """
        if self._enabled_modules_set is None:
            # First, try to load from persisted state file
            persisted = self._load_persisted_module_state()
            if persisted is not None:
                logger.info("Loaded module state from persisted file: %s", persisted)
                self._enabled_modules_set = persisted
            else:
                # Fall back to env var
                raw = self.ENABLED_MODULES.strip()
                # Strip quotes if present (handles both "val" and val)
                if raw.startswith('"') and raw.endswith('"'):
                    raw = raw[1:-1]
                elif raw.startswith("'") and raw.endswith("'"):
                    raw = raw[1:-1]

                if raw:
                    self._enabled_modules_set = {
                        name.strip() for name in raw.split(",") if name.strip()
                    }
                else:
                    self._enabled_modules_set = set()

            # Always include required modules
            for name, module in self.get_modules().items():
                if module.required:
                    self._enabled_modules_set.add(name)

            # Filter out unknown module names
            known_modules = set(self.get_modules().keys())
            self._enabled_modules_set = self._enabled_modules_set & known_modules

        return self._enabled_modules_set

    def is_module_enabled(self, module_name: str) -> bool:
        """Check if a module is currently enabled.

        Required modules are always enabled.
        """
        module = self.get_modules().get(module_name)
        if module and module.required:
            return True
        return module_name in self.get_enabled_module_names()

    def toggle_module(self, module_name: str, enabled: bool) -> bool:
        """Toggle a module on/off at runtime.

        Returns True if the toggle was successful, False otherwise.
        Updates in-memory state AND persists to a state file.
        Required modules cannot be toggled.
        Thread-safe via _toggle_lock.
        """
        module = self.get_modules().get(module_name)
        if module is None:
            logger.warning("Cannot toggle unknown module: %s", module_name)
            return False

        if module.required:
            logger.warning("Cannot toggle required module: %s", module_name)
            return False

        with self._toggle_lock:
            current = self.get_enabled_module_names()

            # Skip no-op toggles
            already_enabled = module_name in current
            if enabled == already_enabled:
                return True

            if enabled:
                current.add(module_name)
            else:
                current.discard(module_name)

            # Update the string representation (sorted for consistency)
            self.ENABLED_MODULES = ",".join(sorted(current))
            self._enabled_modules_set = current

            # Persist to state file (survives container restarts)
            self._persist_module_state()

        return True

    def _get_state_dir(self) -> Path:
        """Get the directory for persistent runtime state files.

        In Docker: /app/state (mounted volume)
        In dev: backend/../state
        """
        candidates = [
            Path("/app/state"),
            Path(__file__).parent.parent.parent / "state",
        ]
        for d in candidates:
            try:
                d.mkdir(parents=True, exist_ok=True)
                # Test write access
                test_file = d / ".write_test"
                test_file.write_text("ok", encoding="utf-8")
                test_file.unlink()
                return d
            except OSError:
                continue

        # Fallback: /tmp (won't persist across restarts but better than nothing)
        fallback = Path("/tmp/realopen-state")
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback

    def _persist_module_state(self) -> None:
        """Persist the enabled modules set to a JSON state file.

        This survives container restarts because the state directory
        is mounted as a Docker volume.
        """
        state_dir = self._get_state_dir()
        state_path = state_dir / "enabled_modules.json"

        try:
            state_data = {
                "enabled_modules": sorted(self.get_enabled_module_names()),
            }
            state_path.write_text(json.dumps(state_data, indent=2), encoding="utf-8")
            logger.info("Persisted module state to %s", state_path)
        except OSError as e:
            logger.error("Failed to persist module state: %s", e)

    def _load_persisted_module_state(self) -> Optional[Set[str]]:
        """Load previously persisted module state from JSON file.

        Returns None if no state file exists.
        """
        state_dir = self._get_state_dir()
        state_path = state_dir / "enabled_modules.json"

        if not state_path.exists():
            return None

        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            modules_list = data.get("enabled_modules", [])
            return set(modules_list)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load persisted module state: %s", e)
            return None

    def _update_env_file(self, key: str, value: str) -> None:
        """Update a single key in the .env file.

        Creates the .env file if it doesn't exist.
        Handles both Docker and dev environments.
        """
        # Try multiple candidate paths for .env
        candidates = [
            Path("/app/.env"),  # Inside Docker container
            Path(__file__).parent.parent.parent / ".env",  # Dev: backend/../.env
        ]

        env_path = None
        for p in candidates:
            if p.exists():
                env_path = p
                break

        # If no .env exists, try to create one from .env.example
        if env_path is None:
            for p in candidates:
                example = p.parent / ".env.example"
                if example.exists():
                    try:
                        import shutil

                        shutil.copy2(str(example), str(p))
                        env_path = p
                        break
                    except OSError:
                        pass

        if env_path is None:
            dev_env = Path(__file__).parent.parent.parent / ".env"
            try:
                dev_env.touch()
                env_path = dev_env
            except OSError as e:
                logger.error("Cannot create .env file: %s", e)
                return

        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
            found = False
            new_lines = []

            import re

            pattern = re.compile(rf"^{re.escape(key)}\s*=")

            for line in lines:
                if pattern.match(line.strip()):
                    new_lines.append(f"{key}={value}")
                    found = True
                else:
                    new_lines.append(line)

            if not found:
                new_lines.append(f"{key}={value}")

            env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            logger.info("Updated %s in %s", key, env_path)

        except OSError as e:
            logger.error("Failed to update .env file: %s", e)

    def get_module_models(self) -> List[ModelConfig]:
        """Get models from all enabled optional modules for the current profile."""
        models = []
        for name, module in self.get_modules().items():
            if not module.required and self.is_module_enabled(name):
                module_models = module.get_models_for_profile(self.HARDWARE_PROFILE)
                models.extend(module_models)
        return models

    def resolve_model(self, model: str) -> str:
        """
        Resolve a model identifier to an actual Ollama model ID.

        Resolution order:
        1. If model is a known role (e.g. "default", "default_vision", "default_code"),
           look it up in the current hardware profile.
        2. If model is a known type (e.g. "chat", "vision", "code"),
           return the model with role="default_<type>" in the current profile.
        3. Check enabled module models for matching role.
        4. Otherwise, treat it as a direct Ollama model ID (e.g. "qwen3:4b").
        """
        profile = self.get_current_profile()

        # Try as a role in the profile
        role_model = profile.get_model_by_role(model)
        if role_model:
            return role_model.id

        # Try as a type (e.g. "vision" -> "default_vision")
        type_model = profile.get_model_by_role(f"default_{model}")
        if type_model:
            return type_model.id

        # Check enabled module models for matching role
        for name, module in self.get_modules().items():
            if self.is_module_enabled(name) and not module.required:
                for m in module.get_models_for_profile(self.HARDWARE_PROFILE):
                    if m.role == model or m.role == f"default_{model}":
                        return m.id

        # Direct model ID - pass through
        return model

    def get_available_models(self) -> List[dict]:
        """Get all available models for the current hardware profile and enabled modules."""
        profile = self.get_current_profile()
        models = [m.to_dict() for m in profile.models]

        # Add models from enabled optional modules
        for name, module in self.get_modules().items():
            if self.is_module_enabled(name) and not module.required:
                module_models = module.get_models_for_profile(self.HARDWARE_PROFILE)
                models.extend(m.to_dict() for m in module_models)

        return models


settings = Settings()
