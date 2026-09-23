#!/bin/bash
set -euo pipefail

# =============================================================================
# RealOpen-AI — Startup Script
#
# This script performs necessary startup tasks for RealOpen-AI, including:
#   - Starting the Ollama server if it's not already running
#
# Usage:
#   ./scripts/start.sh
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
export HF_HOME="${HF_HOME:-${PROJECT_ROOT}/data/huggingface}"

# Create .env file if it doesn't exist
create_env_file() {
    local env_file="${PROJECT_ROOT}/.env"

    if [[ -f "$env_file" ]]; then
        ok ".env file already exists"
        return 0
    fi

    cp "$PROJECT_ROOT/.env.example" "$env_file"
    ok "Created .env from .env.example"
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

start_host_runtime() {
    local url="http://127.0.0.1:8766"
    local log_file="${PROJECT_ROOT}/data/host-runtime.log"
    local pid_file="${PROJECT_ROOT}/data/host-runtime.pid"
    local health_response=""

    host_runtime_is_running() {
        health_response="$(curl -fsS "${url}/health" 2>/dev/null)" || return 1
        HEALTH_RESPONSE="${health_response}" python - <<'PY'
import json
import os
import sys

try:
    health = json.loads(os.environ["HEALTH_RESPONSE"])
except (json.JSONDecodeError, KeyError):
    sys.exit(1)

if isinstance(health, dict) and "docker" in health:
    sys.exit(0)
sys.exit(1)
PY
    }

    if host_runtime_is_running; then
        ok "Host runtime is running"
        return 0
    fi
    if ! command -v poetry &> /dev/null; then
        warn "Poetry is unavailable; host runtime was not started"
        return 0
    fi

    info "Starting host runtime (voice acceleration and coding sandboxes)..."
    (
        cd "${PROJECT_ROOT}/backend"
        nohup poetry run python ../scripts/host-runtime-server.py \
            > "${log_file}" 2>&1 &
        echo $! > "${pid_file}"
    )
    for _ in {1..20}; do
        if host_runtime_is_running; then
            ok "Host runtime is running"
            return 0
        fi
        sleep 0.5
    done
    warn "Host runtime did not start. Run 'make voice-install', then 'make'. See ${log_file}"
}

ensure_sandbox_image() {
    if ! command -v docker &> /dev/null; then
        warn "Docker is unavailable; coding sandboxes will be disabled"
        return 0
    fi
    if docker image inspect realopenai-sandbox:latest &> /dev/null; then
        ok "Sandbox runtime image is available"
        return 0
    fi
    info "Building the sandbox runtime image (first run only)..."
    if docker build -t realopenai-sandbox:latest "${PROJECT_ROOT}/sandbox"; then
        ok "Sandbox runtime image built"
    else
        warn "Sandbox image build failed; run 'make sandbox-build' to retry"
    fi
}

main() {
    create_env_file
    start_ollama
    ensure_sandbox_image
    start_host_runtime
}

main "$@"
