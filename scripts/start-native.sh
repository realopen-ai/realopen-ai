#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if command -v python3.12 >/dev/null 2>&1; then
    exec python3.12 "$ROOT/scripts/run-native.py" "$@"
fi
if ! command -v python3 >/dev/null 2>&1; then
    if [[ "$(uname -s)" == Darwin ]]; then
        if ! command -v brew >/dev/null 2>&1; then
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        fi
        "$(command -v brew || echo /opt/homebrew/bin/brew)" install python@3.12
        export PATH="/opt/homebrew/opt/python@3.12/bin:/usr/local/opt/python@3.12/bin:$PATH"
    elif command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update
        sudo apt-get install -y python3 python3-venv
    else
        echo "Install Python 3.11+ first, then rerun this script." >&2
        exit 1
    fi
fi
exec python3 "$ROOT/scripts/run-native.py" "$@"
