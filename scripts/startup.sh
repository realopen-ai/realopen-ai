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

main() {
    create_env_file
    start_ollama
}

main "$@"