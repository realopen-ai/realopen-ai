#!/bin/bash
set -euo pipefail

# =============================================================================
# RealOpen-AI — First-time setup script
# Prerequisites: Docker, Git, Make, Python3 (user-installed)
# This script: detects hardware, installs Ollama, creates .env, pulls models
# =============================================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILES_YML="$PROJECT_ROOT/profiles.yml"
DATA_DIR="$PROJECT_ROOT/data"

# Ensure data directory exists
mkdir -p "$DATA_DIR"

# Hardware Detection Functions

detect_platform() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        echo "MacOS"
    elif [[ "$OSTYPE" == "linux"* ]]; then
        # Check if we're running under WSL
        if grep -qi microsoft /proc/version 2>/dev/null; then
            echo "Windows (WSL)"
        else
            echo "Linux"
        fi
    elif [[ "$OSTYPE" == "msys"* || "$OSTYPE" == "cygwin"* ]]; then
        echo "Windows"
    else
        echo "Unknown"
    fi
}

detect_ram_gb() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        local ram_bytes=$(sysctl -n hw.memsize)
        echo $((ram_bytes / 1024 / 1024 / 1024))
    else
        local ram_kb=$(grep MemTotal /proc/meminfo | awk '{print $2}')
        echo $((ram_kb / 1024 / 1024))
    fi
}

detect_cpu_cores() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sysctl -n hw.ncpu
    else
        nproc 2>/dev/null || echo 1
    fi
}

detect_gpu() {
    # Returns: type|vram_mb|name
    # type is one of: none, nvidia, apple
    local result="none|0|No GPU detected"

    if command -v nvidia-smi &> /dev/null; then
        local name=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | xargs)
        local vram=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1)
        result="nvidia|${vram:-0}|$name"
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        # Apple Silicon detection — check multiple sources for M-series chips
        local chip=""

        # Try machdep.cpu.brand_string first (works on most macOS versions)
        chip=$(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo "")

        # If brand_string doesn't reveal Apple Silicon, try hw.model
        if [[ "$chip" != *"Apple"* ]] && [[ "$chip" != *"M1"* ]] && [[ "$chip" != *"M2"* ]] && [[ "$chip" != *"M3"* ]] && [[ "$chip" != *"M4"* ]] && [[ "$chip" != *"M5"* ]]; then
            local hw_model=$(sysctl -n hw.model 2>/dev/null || echo "")
            # Macs with Apple Silicon have hw.model starting with "Mac"
            # but we can also check the CPU family via sysctl
            local cpu_family=$(sysctl -n hw.cpufamily 2>/dev/null || echo "0")
            # Apple M1 = 0x1b588bb3, M2 varies, etc.
            # Simpler approach: check if /usr/bin/sysctl reports arm64
            if [[ "$(uname -m)" == "arm64" ]]; then
                # We're on ARM64 macOS — this is Apple Silicon
                # Try to get a more specific chip name
                if [[ -f "/usr/sbin/system_profiler" ]]; then
                    local sp_chip=$(system_profiler SPHardwareDataType 2>/dev/null | grep "Chip:" | awk -F': ' '{print $2}' || echo "")
                    if [[ -n "$sp_chip" ]]; then
                        chip="Apple $sp_chip"
                    else
                        chip="Apple Silicon (ARM64)"
                    fi
                else
                    chip="Apple Silicon (ARM64)"
                fi
            fi
        fi

        # Check for M-series in the chip string
        if [[ "$chip" == *"Apple"* ]] || [[ "$chip" == *"M1"* ]] || [[ "$chip" == *"M2"* ]] || [[ "$chip" == *"M3"* ]] || [[ "$chip" == *"M4"* ]] || [[ "$chip" == *"M5"* ]] || [[ "$chip" == *"Apple Silicon"* ]]; then
            # Apple Silicon uses unified memory — VRAM = RAM
            local ram_bytes=$(sysctl -n hw.memsize)
            local ram_mb=$((ram_bytes / 1024 / 1024))
            result="apple|${ram_mb}|$chip"
        fi
    fi

    echo "$result"
}

get_recommended_profile() {
    local ram_gb=$1
    local gpu_info=$2
    local platform=$3
    local gpu_type=$(echo "$gpu_info" | cut -d'|' -f1)
    local gpu_vram_mb=$(echo "$gpu_info" | cut -d'|' -f2)

    # Apple Silicon (macOS with M-series chip)
    # Apple Silicon uses unified memory — RAM is the deciding factor
    # since GPU shares the same memory pool
    if [[ "$gpu_type" == "apple" ]]; then
        if [[ $ram_gb -le 8 ]]; then
            echo "apple_small"
        elif [[ $ram_gb -le 16 ]]; then
            echo "apple_medium"
        elif [[ $ram_gb -le 32 ]]; then
            echo "apple_large"
        else
            echo "apple_xlarge"
        fi
        return
    fi

    # NVIDIA GPU (CUDA)
    # For NVIDIA, VRAM is the key bottleneck — the model needs to fit in VRAM
    # for GPU-accelerated inference. System RAM matters less.
    if [[ "$gpu_type" == "nvidia" ]]; then
        if [[ $gpu_vram_mb -le 6144 ]]; then
            echo "nvidia_small"
        elif [[ $gpu_vram_mb -le 12288 ]]; then
            echo "nvidia_medium"
        elif [[ $gpu_vram_mb -le 24576 ]]; then
            echo "nvidia_large"
        else
            echo "nvidia_xlarge"
        fi
        return
    fi

    # CPU-only (no GPU detected)
    # CPU-only inference is RAM-bound — the model needs to fit in system RAM
    # Beyond 16 GB, CPU inference on larger models becomes impractically slow
    if [[ $ram_gb -le 8 ]]; then
        echo "cpu_small"
    else
        echo "cpu_medium"
    fi
}

# Profiles.yml Helper
# Uses Python3 to parse YAML
get_default_model() {
    local profile=$1
    python3 "$PROJECT_ROOT/scripts/profile-helper.py" "$profile" default-model 2>/dev/null || echo "qwen3:4b"
}

get_all_models() {
    local profile=$1
    python3 "$PROJECT_ROOT/scripts/profile-helper.py" "$profile" all-models 2>/dev/null || echo "qwen3:4b"
}

get_profile_label() {
    local profile=$1
    python3 "$PROJECT_ROOT/scripts/profile-helper.py" "$profile" label 2>/dev/null || echo "$profile"
}

# Voice (ASR/TTS) models for a profile — resolved from profiles.yml
# (single source of truth: changing profiles.yml changes what gets installed,
# no script edits needed).
get_voice_models() {
    local profile=$1
    python3 "$PROJECT_ROOT/scripts/profile-helper.py" "$profile" voice-models 2>/dev/null || echo ""
}

get_voice_summary() {
    local profile=$1
    python3 "$PROJECT_ROOT/scripts/profile-helper.py" "$profile" voice-summary 2>/dev/null || echo ""
}

# Prerequisite Checks
check_prerequisites() {
    echo ""
    info "Checking prerequisites..."

    local missing=()

    if ! command -v docker &> /dev/null; then
        missing+=("Docker")
    else
        ok "Docker $(docker --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')"
    fi

    if ! command -v git &> /dev/null; then
        missing+=("Git")
    else
        ok "Git $(git --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')"
    fi

    if ! command -v make &> /dev/null; then
        missing+=("Make")
    else
        ok "Make $(make --version | head -1 | grep -oE '[0-9]+\.[0-9]+')"
    fi

    if ! command -v python3 &> /dev/null; then
        missing+=("Python3")
    else
        ok "Python3 $(python3 --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')"
    fi

    if [[ ${#missing[@]} -gt 0 ]]; then
        error "Missing prerequisites: ${missing[*]}"
        echo ""
        echo "  Please install them before continuing:"
        echo "  - Docker:  https://docs.docker.com/get-docker/"
        echo "  - Git:     https://git-scm.com/downloads"
        echo "  - Make:    'xcode-select --install' (macOS) or 'sudo apt install build-essential' (Linux)"
        echo "  - Python3: 'brew install python3' (macOS) or 'sudo apt install python3' (Linux)"
        exit 1
    fi

    # Check Docker is running
    if ! docker info &> /dev/null; then
        error "Docker is installed but not running. Please start Docker Desktop."
        exit 1
    fi
    ok "Docker daemon is running"

    # Validate profiles.yml
    if [[ -f "$PROFILES_YML" ]]; then
        # Validate with the first profile name (cpu_small) — validation checks ALL profiles
        python3 "$PROJECT_ROOT/scripts/profile-helper.py" "cpu_small" validate &>/dev/null && ok "profiles.yml is valid"
    else
        error "profiles.yml not found at $PROFILES_YML"
        exit 1
    fi
}

# Install Ollama
install_ollama() {
    echo ""
    info "Checking for Ollama..."

    if command -v ollama &> /dev/null; then
        ok "Ollama is installed: $(ollama --version 2>/dev/null || echo 'version unknown')"
        return 0
    fi

    warn "Ollama not found. Installing..."

    if [[ "$OSTYPE" == "darwin"* ]]; then
        if command -v brew &> /dev/null; then
            info "Installing Ollama via Homebrew..."
            brew install ollama
        else
            info "Installing Ollama via official script..."
            curl -fsSL https://ollama.com/install.sh | sh
        fi
    else
        info "Installing Ollama via official script..."
        curl -fsSL https://ollama.com/install.sh | sh
    fi

    if command -v ollama &> /dev/null; then
        ok "Ollama installed successfully"
    else
        error "Failed to install Ollama. Please install manually: https://ollama.com"
        exit 1
    fi
}

# Start Ollama Service
start_ollama() {
    if curl -s http://localhost:11434/api/version &> /dev/null; then
        ok "Ollama server is running"
        return 0
    fi

    info "Starting Ollama server..."

    if [[ "$OSTYPE" == "darwin"* ]]; then
        if pgrep -x "Ollama" &> /dev/null; then
            ok "Ollama app is running"
        else
            nohup ollama serve > /dev/null 2>&1 &
            sleep 2
        fi
    else
        nohup ollama serve > /dev/null 2>&1 &
        sleep 2
    fi

    if curl -s http://localhost:11434/api/version &> /dev/null; then
        ok "Ollama server is running"
    else
        warn "Ollama server may still be starting. You can start it manually with: ollama serve"
    fi
}

# Create .env
create_env_file() {
    echo ""
    info "Configuring environment..."

    local env_file="$PROJECT_ROOT/.env"

    if [[ -f "$env_file" ]]; then
        ok ".env file already exists"
        return 0
    fi

    cp "$PROJECT_ROOT/.env.example" "$env_file"
    ok "Created .env from .env.example"
}

# Detect Hardware & Configure
detect_and_configure() {
    echo ""
    info "Detecting hardware..."

    local platform=$(detect_platform)
    local ram_gb=$(detect_ram_gb)
    local cpu_cores=$(detect_cpu_cores)
    local gpu_info=$(detect_gpu)
    local gpu_type=$(echo "$gpu_info" | cut -d'|' -f1)
    local gpu_vram_mb=$(echo "$gpu_info" | cut -d'|' -f2)
    local gpu_name=$(echo "$gpu_info" | cut -d'|' -f3)
    local profile=$(get_recommended_profile "$ram_gb" "$gpu_info" "$platform")
    local profile_label=$(get_profile_label "$profile")
    local default_model=$(get_default_model "$profile")
    local all_models=$(get_all_models "$profile")
    local voice_models=$(get_voice_models "$profile")

    # Determine platform display string
    local platform_display="$platform"
    case "$platform" in
        macos)  platform_display="macOS" ;;
        linux)  platform_display="Linux" ;;
        wsl)    platform_display="Windows (WSL)" ;;
        windows) platform_display="Windows" ;;
    esac

    # Determine GPU type display
    local gpu_type_display="None (CPU-only)"
    case "$gpu_type" in
        nvidia) gpu_type_display="NVIDIA (CUDA)" ;;
        apple)  gpu_type_display="Apple Silicon (Metal/MLX)" ;;
    esac

    # Calculate VRAM display
    local vram_display="N/A"
    if [[ "$gpu_type" == "nvidia" ]]; then
        vram_display="${gpu_vram_mb} MB"
    elif [[ "$gpu_type" == "apple" ]]; then
        vram_display="Unified (${ram_gb} GB)"
    fi

    echo ""
    echo "  ┌──────────────────────────────────────────────┐"
    echo "  │         Hardware Detection Results           │"
    echo "  ├──────────────────────────────────────────────┤"
    printf "  │  Platform:       %-28s│\n" "$platform_display"
    printf "  │  RAM:            %-28s│\n" "${ram_gb} GB"
    printf "  │  CPU Cores:      %-28s│\n" "$cpu_cores"
    printf "  │  GPU Type:       %-28s│\n" "$gpu_type_display"
    printf "  │  GPU:            %-28s│\n" "$gpu_name"
    printf "  │  VRAM:           %-28s│\n" "$vram_display"
    echo "  ├──────────────────────────────────────────────┤"
    printf "  │  Profile:        %-29s│\n" "$profile_label"
    echo "  ├──────────────────────────────────────────────┤"
    echo "  │  Models to pull:                             │"
    while IFS= read -r model; do
        printf "  │    - %-40s│\n" "$model"
    done <<< "$all_models"
    if [[ -n "$voice_models" ]]; then
        echo "  ├──────────────────────────────────────────────┤"
        echo "  │  Voice models (ASR + TTS):                   │"
        while IFS= read -r model; do
            printf "  │    - %-40s│\n" "$model"
        done <<< "$voice_models"
    fi
    echo "  └──────────────────────────────────────────────┘"
    echo ""

    # Update .env with detected values
    local env_file="$PROJECT_ROOT/.env"
    if [[ -f "$env_file" ]]; then
        if grep -q "^HARDWARE_PROFILE=" "$env_file"; then
            sed -i.bak "s/^HARDWARE_PROFILE=.*/HARDWARE_PROFILE=${profile}/" "$env_file"
        fi
        if grep -q "^DEFAULT_MODEL=" "$env_file"; then
            sed -i.bak "s/^DEFAULT_MODEL=.*/DEFAULT_MODEL=${default_model}/" "$env_file"
        fi
        if grep -q "^ENABLED_MODULES=" "$env_file"; then
            sed -i.bak "s/^ENABLED_MODULES=.*/ENABLED_MODULES=assistant/" "$env_file"
        fi
        if [[ "$platform" == "linux" || "$platform" == "wsl" ]]; then
            if grep -q "^OLLAMA_BASE_URL=" "$env_file"; then
                sed -i.bak "s|^OLLAMA_BASE_URL=.*|OLLAMA_BASE_URL=http://172.17.0.1:11434|" "$env_file"
            fi
        fi
        rm -f "$env_file.bak"
        ok "Updated .env with hardware profile: $profile, default model: $default_model"
    fi

    # Write hardware.json to data/ folder (used by web setup wizard)
    bash "$PROJECT_ROOT/scripts/detect-hardware.sh" "$DATA_DIR"
    ok "Wrote hardware info to data/hardware.json"
}

# Pull ALL Models for Profile (including optional module models)
pull_models() {
    echo ""

    local profile
    profile=$(grep "^HARDWARE_PROFILE=" "$PROJECT_ROOT/.env" 2>/dev/null | cut -d'=' -f2 || echo "cpu_small")

    info "Checking models for profile '$profile'..."

    # Check if Ollama is running
    if ! curl -s http://localhost:11434/api/version &> /dev/null; then
        warn "Ollama server is not running. Skipping model pulls."
        warn "Start Ollama and run: make pull-models"
        return 0
    fi

    # Pull profile models (from profiles.yml — required module models)
    local all_models
    all_models=$(get_all_models "$profile")

    while IFS= read -r model; do
        if ollama list 2>/dev/null | awk '{print $1}' | grep -Fxq "$model"; then
            ok "Model '$model' is already available"
        else
            info "Pulling model '$model' (this may take a while on first run)..."
            ollama pull "$model"
            if ollama list 2>/dev/null | awk '{print $1}' | grep -Fxq "$model"; then
                ok "Model '$model' pulled successfully"
            else
                warn "Model pull may have failed. You can try manually: ollama pull $model"
            fi
        fi
    done <<< "$all_models"

    # Pull optional module models (from modules.yml)
    local enabled_modules_str
    enabled_modules_str=$(grep "^ENABLED_MODULES=" "$PROJECT_ROOT/.env" 2>/dev/null | cut -d'=' -f2 || echo "assistant")

    info "Checking optional module models for enabled modules: $enabled_modules_str"

    local module_models
    module_models=$(python3 "$PROJECT_ROOT/scripts/profile-helper.py" modules "$profile" all-module-models 2>/dev/null || echo "")

    if [[ -n "$module_models" ]]; then
        # Check which optional modules are enabled and pull their models
        while IFS= read -r model; do
            if [[ -z "$model" ]]; then
                continue
            fi
            if ollama list 2>/dev/null | awk '{print $1}' | grep -Fxq "$model"; then
                ok "Module model '$model' is already available"
            else
                info "Pulling module model '$model'..."
                ollama pull "$model"
                if ollama list 2>/dev/null | awk '{print $1}' | grep -Fxq "$model"; then
                    ok "Module model '$model' pulled successfully"
                else
                    warn "Module model pull may have failed. You can try manually: ollama pull $model"
                fi
            fi
        done <<< "$module_models"
    fi
}

# ─── Install Voice Models (ASR + TTS from profiles.yml) ────────────
# Runs the host-side stdlib-only installer. Voice models land in
# $DATA_DIR/models/voice (persisted Docker bind mount) with a manifest so
# the web wizard (and the backend) see them as installed/idempotent.
# Failures are tolerated with a warning — the web setup wizard can retry.
install_voice_models() {
    echo ""

    local profile
    profile=$(grep "^HARDWARE_PROFILE=" "$PROJECT_ROOT/.env" 2>/dev/null | cut -d'=' -f2 || echo "cpu_small")

    info "Installing voice models (ASR + TTS) for profile '$profile'..."

    # Show the plan (resolved from profiles.yml — single source of truth)
    local voice_summary
    voice_summary=$(get_voice_summary "$profile")
    if [[ -n "$voice_summary" ]]; then
        echo "$voice_summary"
    else
        warn "No voice models configured in profiles.yml — skipping."
        return 0
    fi

    if python3 "$PROJECT_ROOT/scripts/install-voice-models.py" --profile "$profile" --data-dir "$DATA_DIR"; then
        ok "Voice models installed to $DATA_DIR/models/voice"
    else
        warn "Voice model installation reported failures."
        warn "You can retry from the web setup wizard, or run:"
        warn "  python3 scripts/install-voice-models.py --profile $profile"
    fi
}

# ─── Check if setup is needed ────────────────────────────────────────

is_setup_needed() {
    # Setup is needed if data/.setup-complete does NOT exist
    if [[ -f "$DATA_DIR/.setup-complete" ]]; then
        return 1  # Setup already done
    fi
    return 0  # Setup needed
}

# Main
main() {
    echo ""
    echo "╔═══════════════════════════════════════════╗"
    echo "║       RealOpen-AI — Setup Wizard          ║"
    echo "╚═══════════════════════════════════════════╝"

    # Check if setup is already complete
    if ! is_setup_needed; then
        local existing_profile
        existing_profile=$(grep "^HARDWARE_PROFILE=" "$PROJECT_ROOT/.env" 2>/dev/null | cut -d'=' -f2 || echo "cpu_small")
        ok "Setup already complete (profile: $existing_profile)"
        ok "Skipping setup. Run 'make reset-setup' to re-run setup."
        return 0
    fi

    check_prerequisites
    install_ollama
    start_ollama
    create_env_file
    detect_and_configure
    pull_models
    install_voice_models

    echo ""
    echo "╔═══════════════════════════════════════════╗"
    echo "║           Setup Complete!                 ║"
    echo "╠═══════════════════════════════════════════╣"
    echo "║  Development:  make dev                   ║"
    echo "║  Production:   make up                    ║"
    echo "║  Monitoring:   make monitor               ║"
    echo "╚═══════════════════════════════════════════╝"
    echo ""

    # Create setup-complete marker in data/ folder
    # Read profile from .env (written by detect_and_configure)
    local profile
    profile=$(grep "^HARDWARE_PROFILE=" "$PROJECT_ROOT/.env" 2>/dev/null | cut -d'=' -f2 || echo "cpu_small")
    local marker="$DATA_DIR/.setup-complete"
    echo "{\"profile\": \"${profile}\", \"enabled_modules\": [\"assistant\"], \"source\": \"cli\", \"voice\": true}" > "$marker"
    ok "Created data/.setup-complete marker"
}

main "$@"