"""Resolve native settings without rewriting the user's Docker .env."""

import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit, unquote

from .processes import RuntimeLock

ROOT = Path(__file__).resolve().parents[2]
DEFAULTS = {
    "DATABASE_URL": "postgresql+asyncpg://realopen:realopen@postgres:5432/realopen",
    "REDIS_URL": "redis://redis:6379/0",
    "OLLAMA_BASE_URL": "http://host.docker.internal:11434",
    "VOICE_RUNTIME_URL": "http://host.docker.internal:8766",
    "SEARXNG_BASE_URL": "http://searxng:8080",
    "DEGOOG_BASE_URL": "http://degoog:4444",
}


def dotenv(path):
    result = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip().removeprefix("export ")
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].rstrip()
            result[key.strip()] = value
    return result


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            json.dump(value, output, indent=2)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class Config:
    def __init__(self, root=ROOT, environ=None):
        self.root = Path(root).resolve()
        self.data = self.root / "data"
        self.runtime = self.data / "native"
        self.env = {
            **dotenv(self.root / ".env"),
            **dotenv(self.root / ".env.native"),
            **(os.environ if environ is None else environ),
        }
        tag = f"{platform.system().lower()}-{platform.machine().lower()}-py312"
        self.envs = self.runtime / "envs" / tag
        self.custom_python = bool(self.env.get("NATIVE_PYTHON"))
        self.python = Path(
            self.env.get("NATIVE_PYTHON")
            or self.envs
            / "backend"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        secret_path = self.runtime / "credentials.json"
        self.runtime.mkdir(parents=True, exist_ok=True)
        with RuntimeLock(self.runtime / "credentials.lock"):
            if not secret_path.exists():
                atomic_json(secret_path, {"postgres_password": secrets.token_hex(24)})
            password = json.loads(secret_path.read_text())["postgres_password"]
        local = {
            "DATABASE_URL": f"postgresql+asyncpg://realopen:{password}@127.0.0.1:55432/realopen",
            "REDIS_URL": "redis://127.0.0.1:56379/0",
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "VOICE_RUNTIME_URL": "http://127.0.0.1:8766",
            "SEARXNG_BASE_URL": "http://127.0.0.1:8080",
            "DEGOOG_BASE_URL": "http://127.0.0.1:4444",
        }
        self.managed = {}
        for key, default in local.items():
            supplied = self.env.get(key)
            automatic = not supplied or supplied.rstrip("/") in {DEFAULTS[key], default}
            mode = self.env.get(
                "NATIVE_"
                + key.removesuffix("_BASE_URL").removesuffix("_URL")
                + "_MODE",
                "auto",
            )
            if mode not in {"auto", "managed", "external"}:
                raise ValueError(f"Invalid native service mode for {key}: {mode}")
            self.managed[key] = mode == "managed" or (mode == "auto" and automatic)
            self.env[key] = default if automatic else supplied
            if self.managed[key] and urlsplit(self.env[key]).hostname not in {
                "127.0.0.1",
                "localhost",
                "::1",
            }:
                raise ValueError(f"Managed {key} must use a loopback endpoint")
        self.env.update(
            REALOPEN_RUNTIME="native",
            REALOPEN_DATA_DIR=str(self.data),
            REALOPEN_STATE_DIR=str(self.data / "state"),
            HF_HOME=self.env.get("HF_HOME", str(self.data / "huggingface")),
            PIP_CACHE_DIR=str(self.data / "pip-cache"),
            VITE_PROXY_TARGET=f"http://127.0.0.1:{self.env.get('NATIVE_API_PORT', '8000')}",
        )
        for key, value in list(self.env.items()):
            if value == "/app/data" or value.startswith("/app/data/"):
                self.env[key] = str(self.data) + value[len("/app/data") :]
        self.env["PATH"] = os.pathsep.join(
            [
                str(self.python.parent),
                str(self.runtime / "tools/node/bin"),
                str(self.runtime / "tools/node"),
                str(self.runtime / "tools/bun/node_modules/.bin"),
                str(Path.home() / ".bun/bin"),
                "/opt/homebrew/bin",
                "/usr/local/bin",
                self.env.get("PATH", os.defpath),
            ]
        )
        self.env["NATIVE_PYTHON"] = str(self.python)
        for name in ("searxng", "degoog"):
            self.env[f"NATIVE_MANAGED_{name.upper()}"] = (
                "1" if self.managed[name.upper() + "_BASE_URL"] else "0"
            )
        self.env["PYTHONUNBUFFERED"] = "1"
        self.env.setdefault("AGENT_TRACE_DIR", str(self.data / "agent_traces"))
        self.env["PYTHONPATH"] = str(self.root / "backend")
        if platform.system() == "Darwin":
            self.env["DYLD_FALLBACK_LIBRARY_PATH"] = os.pathsep.join(
                [
                    "/opt/homebrew/lib",
                    "/usr/local/lib",
                    self.env.get("DYLD_FALLBACK_LIBRARY_PATH", "/usr/lib"),
                ]
            )

    def tool(self, name):
        binary = shutil.which(name, path=self.env["PATH"])
        if not binary:
            raise RuntimeError(
                f"Missing executable: {name}. Run start-native setup, or install it and add it to PATH."
            )
        return binary

    def postgres_bin(self, name):
        configured = self.env.get("NATIVE_PG_BIN")
        if configured:
            binary = Path(configured) / (name + ".exe" if os.name == "nt" else name)
            if not binary.is_file():
                raise RuntimeError(f"Configured NATIVE_PG_BIN is missing {name}")
            return str(binary)
        candidates = [Path(configured)] if configured else []
        for base in [
            "/opt/homebrew/opt/postgresql@17/bin",
            "/usr/local/opt/postgresql@17/bin",
            "/usr/lib/postgresql/17/bin",
            "C:/Program Files/PostgreSQL/17/bin",
        ]:
            candidates.append(Path(base))
        pg_config = shutil.which("pg_config", path=self.env["PATH"])
        if pg_config:
            candidates.append(
                Path(
                    subprocess.check_output([pg_config, "--bindir"], text=True).strip()
                )
            )
        for directory in candidates:
            path = directory / (name + ".exe" if os.name == "nt" else name)
            if path.exists():
                return str(path)
        return self.tool(name)

    def pg_env(self):
        parsed = urlsplit(
            self.env["DATABASE_URL"].replace("postgresql+asyncpg:", "postgresql:")
        )
        return {
            **self.env,
            "PGHOST": parsed.hostname or "127.0.0.1",
            "PGPORT": str(parsed.port or 5432),
            "PGUSER": unquote(parsed.username or "realopen"),
            "PGPASSWORD": unquote(parsed.password or ""),
            "PGDATABASE": parsed.path.lstrip("/") or "realopen",
        }
