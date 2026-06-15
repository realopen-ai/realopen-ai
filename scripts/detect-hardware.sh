#!/bin/bash
# =============================================================================
# RealOpen-AI — Hardware Detection Script
#
# Outputs a JSON file (data/hardware.json) with everything the setup wizard
# needs to know about the host hardware: platform, RAM, CPU, GPU, and
# the recommended hardware profile.
#
# Usage:
#   ./scripts/detect-hardware.sh                # writes data/hardware.json in project root
#   ./scripts/detect-hardware.sh /path          # writes hardware.json at /path/data/
# =============================================================================

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-$PROJECT_ROOT}"

# If the output dir is the project root, use data/ subfolder
if [[ "$OUTPUT_DIR" == "$PROJECT_ROOT" ]]; then
    DATA_DIR="$OUTPUT_DIR/data"
else
    DATA_DIR="$OUTPUT_DIR"
fi

mkdir -p "$DATA_DIR"
OUTPUT_FILE="$DATA_DIR/hardware.json"

# ─── Detection Functions ──────────────────────────────────────────

detect_platform() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        echo "macos"
    elif [[ "$OSTYPE" == "linux"* ]]; then
        if grep -qi microsoft /proc/version 2>/dev/null; then
            echo "wsl"
        else
            echo "linux"
        fi
    elif [[ "$OSTYPE" == "msys"* || "$OSTYPE" == "cygwin"* ]]; then
        echo "windows"
    else
        echo "unknown"
    fi
}

detect_platform_display() {
    case "$(detect_platform)" in
        macos)    echo "macOS" ;;
        linux)    echo "Linux" ;;
        wsl)      echo "Windows (WSL)" ;;
        windows)  echo "Windows" ;;
        *)        echo "Unknown" ;;
    esac
}

detect_ram_gb() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        local ram_bytes
        ram_bytes=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
        echo $((ram_bytes / 1024 / 1024 / 1024))
    else
        local ram_kb
        ram_kb=$(grep MemTotal /proc/meminfo 2>/dev/null | awk '{print $2}' || echo 0)
        echo $((ram_kb / 1024 / 1024))
    fi
}

detect_cpu_cores() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sysctl -n hw.ncpu 2>/dev/null || echo 1
    else
        nproc 2>/dev/null || echo 1
    fi
}

detect_cpu_name() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sysctl -n machdep.cpu.brand_string 2>/dev/null || echo "Unknown CPU"
    else
        grep "model name" /proc/cpuinfo 2>/dev/null | head -1 | cut -d':' -f2 | xargs || echo "Unknown CPU"
    fi
}

detect_gpu() {
    # Returns JSON-friendly values: type, vram_mb, name
    local gpu_type="none"
    local gpu_vram_mb=0
    local gpu_name="No GPU detected"

    if command -v nvidia-smi &> /dev/null; then
        gpu_type="nvidia"
        gpu_name=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | xargs || echo "NVIDIA GPU")
        gpu_vram_mb=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 || echo 0)
    elif [[ "$OSTYPE" == "darwin"* ]] && [[ "$(uname -m)" == "arm64" ]]; then
        # Apple Silicon detection
        gpu_type="apple"
        local chip
        if command -v system_profiler &> /dev/null; then
            chip=$(system_profiler SPHardwareDataType 2>/dev/null | grep "Chip:" | awk -F': ' '{print $2}' || echo "")
        fi
        if [[ -z "$chip" ]]; then
            chip="Apple Silicon"
        fi
        gpu_name="Apple $chip"
        # Unified memory — VRAM = RAM
        local ram_bytes
        ram_bytes=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
        gpu_vram_mb=$((ram_bytes / 1024 / 1024))
    fi

    echo "{\"type\": \"$gpu_type\", \"vram_mb\": $gpu_vram_mb, \"name\": \"$gpu_name\"}"
}

get_recommended_profile() {
    local ram_gb=$1
    local gpu_type=$2
    local gpu_vram_mb=$3

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

    # CPU-only
    if [[ $ram_gb -le 8 ]]; then
        echo "cpu_small"
    else
        echo "cpu_medium"
    fi
}

# ─── Check Ollama ────────────────────────────────────────────────

check_ollama() {
    if command -v ollama &> /dev/null; then
        echo "true"
    else
        echo "false"
    fi
}

check_ollama_running() {
    if curl -s http://localhost:11434/api/version &> /dev/null 2>&1; then
        echo "true"
    else
        echo "false"
    fi
}

check_docker() {
    if command -v docker &> /dev/null && docker info &> /dev/null 2>&1; then
        echo "true"
    else
        echo "false"
    fi
}

# ─── Main ────────────────────────────────────────────────────────

platform=$(detect_platform)
platform_display=$(detect_platform_display)
ram_gb=$(detect_ram_gb)
cpu_cores=$(detect_cpu_cores)
cpu_name=$(detect_cpu_name)
gpu_json=$(detect_gpu)

gpu_type=$(echo "$gpu_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['type'])" 2>/dev/null || echo "none")
gpu_vram_mb=$(echo "$gpu_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['vram_mb'])" 2>/dev/null || echo 0)
gpu_name=$(echo "$gpu_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['name'])" 2>/dev/null || echo "Unknown")

gpu_vram_gb=$((gpu_vram_mb / 1024))

recommended_profile=$(get_recommended_profile "$ram_gb" "$gpu_type" "$gpu_vram_mb")
ollama_installed=$(check_ollama)
ollama_running=$(check_ollama_running)
docker_available=$(check_docker)

# Build the JSON output
cat > "$OUTPUT_FILE" << EOF
{
  "platform": "$platform",
  "platform_display": "$platform_display",
  "ram_gb": $ram_gb,
  "cpu_cores": $cpu_cores,
  "cpu_name": "$cpu_name",
  "gpu_type": "$gpu_type",
  "gpu_vram_mb": $gpu_vram_mb,
  "gpu_vram_gb": $gpu_vram_gb,
  "gpu_name": "$gpu_name",
  "recommended_profile": "$recommended_profile",
  "ollama_installed": $ollama_installed,
  "ollama_running": $ollama_running,
  "docker_available": $docker_available
}
EOF


echo "Detected Hardware Summary:"
echo ""

echo "  Platform: $platform_display"
echo "  RAM: ${ram_gb} GB"
echo "  CPU: $cpu_name ($cpu_cores cores)"
if [[ "$gpu_type" != "none" ]]; then
    echo "  GPU: $gpu_name ($gpu_vram_gb GB VRAM)"
else
    echo "  GPU: None detected"
fi
echo ""

echo "  Recommended Profile: $recommended_profile"
echo ""

if [[ "$ollama_installed" == "true" ]]; then
    echo "  Ollama: Installed"
    if [[ "$ollama_running" == "true" ]]; then
        echo "  Ollama Server: Running"
    else
        echo "  Ollama Server: Not running"
    fi
else
    echo "  Ollama: Not installed"
fi
if [[ "$docker_available" == "true" ]]; then
    echo "  Docker: Available"
else
    echo "  Docker: Not available"
fi

echo ""
echo "Hardware detection complete. Results written to: $OUTPUT_FILE"