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
NC='\033[0m' # No Color

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILES_YML="$PROJECT_ROOT/profiles.yml"

# ── Hardware Detection Functions ──
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
    local result="none|0|No GPU detected"
    
    if command -v nvidia-smi &> /dev/null; then
        local name=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | xargs)
        local vram=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1)
        result="nvidia|${vram:-0}|$name"
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        local chip=$(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo "")
        if [[ "$chip" == *"Apple"* ]] || [[ "$chip" == *"M1"* ]] || [[ "$chip" == *"M2"* ]] || [[ "$chip" == *"M3"* ]] || [[ "$chip" == *"M4"* ]]; then
            result="apple|unified|$chip"
        fi
    fi
    
    echo "$result"
}

get_recommended_profile() {
    local ram_gb=$1
    local gpu_info=$2
    local gpu_type=$(echo "$gpu_info" | cut -d'|' -f1)
    local gpu_vram=$(echo "$gpu_info" | cut -d'|' -f2)
    
    local effective_ram=$ram_gb
    
    if [[ "$gpu_type" == "nvidia" ]] && [[ "$gpu_vram" != "unknown" ]] && [[ "$gpu_vram" -gt 4000 ]]; then
        effective_ram=$((ram_gb + gpu_vram / 1024 / 2))
    fi
    
    if [[ $effective_ram -lt 10 ]]; then
        echo "8gb"
    elif [[ $effective_ram -lt 20 ]]; then
        echo "16gb"
    elif [[ $effective_ram -lt 40 ]]; then
        echo "32gb"
    else
        echo "64gb"
    fi
}

# ── Profiles.yml Helper ──
# Uses Python3 to parse YAML
get_default_model() {
    local profile=$1
    python3 "$PROJECT_ROOT/scripts/profile-helper.py" "$profile" default-model 2>/dev/null || echo "qwen3:7b"
}

get_all_models() {
    local profile=$1
    python3 "$PROJECT_ROOT/scripts/profile-helper.py" "$profile" all-models 2>/dev/null || echo "qwen3:7b"
}

# ── Prerequisite Checks ──
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
        python3 "$PROJECT_ROOT/scripts/profile-helper.py" "8gb" validate &>/dev/null && ok "profiles.yml is valid"
    else
        error "profiles.yml not found at $PROFILES_YML"
        exit 1
    fi
}

# ── Install Ollama ──
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

# ── Start Ollama Service ──
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

# ── Create .env ──
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

# ── Detect Hardware & Configure ──
detect_and_configure() {
    echo ""
    info "Detecting hardware..."
    
    local ram_gb=$(detect_ram_gb)
    local cpu_cores=$(detect_cpu_cores)
    local gpu_info=$(detect_gpu)
    local gpu_type=$(echo "$gpu_info" | cut -d'|' -f1)
    local gpu_vram=$(echo "$gpu_info" | cut -d'|' -f2)
    local gpu_name=$(echo "$gpu_info" | cut -d'|' -f3)
    local profile=$(get_recommended_profile "$ram_gb" "$gpu_info")
    local default_model=$(get_default_model "$profile")
    local all_models=$(get_all_models "$profile")
    
    echo ""
    echo "  ┌───────────────────────────────────────┐"
    echo "  │      Hardware Detection Results       │"
    echo "  ├───────────────────────────────────────┤"
    echo "  │  RAM:            ${ram_gb} GB$(printf '%*s' $((18 - ${#ram_gb})) '')│"
    echo "  │  CPU Cores:      ${cpu_cores}$(printf '%*s' $((21 - ${#cpu_cores})) '')│"
    echo "  │  GPU:            ${gpu_name}$(printf '%*s' $((21 - ${#gpu_name})) '')│"
    echo "  ├───────────────────────────────────────┤"
    echo "  │  Profile:        ${profile}$(printf '%*s' $((21 - ${#profile})) '')│"
    echo "  │  Default Model:  ${default_model}$(printf '%*s' $((21 - ${#default_model})) '')│"
    echo "  ├───────────────────────────────────────┤"
    echo "  │  Models to pull:                      │"
    while IFS= read -r model; do
        echo "  │    - $model$(printf '%*s' $((33 - ${#model})) '')│"
    done <<< "$all_models"
    echo "  └───────────────────────────────────────┘"
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
        if [[ "$OSTYPE" == "linux"* ]]; then
            if grep -q "^OLLAMA_BASE_URL=" "$env_file"; then
                sed -i.bak "s|^OLLAMA_BASE_URL=.*|OLLAMA_BASE_URL=http://172.17.0.1:11434|" "$env_file"
            fi
        fi
        rm -f "$env_file.bak"
        ok "Updated .env with hardware profile: $profile, default model: $default_model"
    fi
}

# ── Pull ALL Models for Profile ──
pull_models() {
    echo ""
    
    local profile
    profile=$(grep "^HARDWARE_PROFILE=" "$PROJECT_ROOT/.env" 2>/dev/null | cut -d'=' -f2 || echo "8gb")
    
    info "Checking models for profile '$profile'..."
    
    # Check if Ollama is running
    if ! curl -s http://localhost:11434/api/version &> /dev/null; then
        warn "Ollama server is not running. Skipping model pulls."
        warn "Start Ollama and run: make pull-model"
        return 0
    fi
    
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
}

# ── Main ──
main() {
    echo ""
    echo "╔═══════════════════════════════════════════╗"
    echo "║       RealOpen-AI — Setup Wizard          ║"
    echo "╚═══════════════════════════════════════════╝"
    
    check_prerequisites
    install_ollama
    start_ollama
    create_env_file
    detect_and_configure
    pull_models
    
    echo ""
    echo "╔═══════════════════════════════════════════╗"
    echo "║           Setup Complete!                 ║"
    echo "╠═══════════════════════════════════════════╣"
    echo "║  Development:  make dev                   ║"
    echo "║  Production:   make up                    ║"
    echo "║  Monitoring:   make monitor               ║"
    echo "╚═══════════════════════════════════════════╝"
    echo ""
}

main "$@"