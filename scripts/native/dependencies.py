"""Install missing dependencies into private environments; preserve user installs."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import venv
import zipfile

from .config import atomic_json
from .output import log


def run(command, cfg, cwd=None, env=None):
    log(" ".join(str(x) for x in command), "run")
    subprocess.run(
        [str(x) for x in command], cwd=cwd or cfg.root, env=env or cfg.env, check=True
    )


def download(url, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response, destination.open(
        "wb"
    ) as target:
        shutil.copyfileobj(response, target)


class Installer:
    def __init__(self, cfg):
        self.cfg = cfg
        # Avoid unrelated package-manager updates/cleanup during app setup.
        cfg.env.setdefault("HOMEBREW_NO_AUTO_UPDATE", "1")
        cfg.env.setdefault("HOMEBREW_NO_INSTALL_CLEANUP", "1")

    def apt(self, packages):
        missing = [
            p
            for p in packages
            if subprocess.run(
                ["dpkg-query", "-W", "-f=${Status}", p], capture_output=True, text=True
            ).stdout.strip()
            != "install ok installed"
        ]
        if missing:
            sudo = [] if hasattr(os, "geteuid") and os.geteuid() == 0 else ["sudo"]
            run([*sudo, "apt-get", "update"], self.cfg)
            run([*sudo, "apt-get", "install", "-y", "--no-upgrade", *missing], self.cfg)

    def system(self):
        cfg = self.cfg
        if sys.platform == "darwin":
            if not shutil.which("brew", path=cfg.env["PATH"]):
                path = cfg.runtime / "install-homebrew.sh"
                download(
                    "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh",
                    path,
                )
                run(["/bin/bash", path], cfg)
            packages = ["git", "curl", "pango", "harfbuzz", "fontconfig", "tesseract"]
            if cfg.managed["DATABASE_URL"]:
                try:
                    cfg.postgres_bin("initdb")
                except RuntimeError:
                    if cfg.env.get("NATIVE_PG_BIN"):
                        raise
                    packages.append("postgresql@17")
            if cfg.managed["REDIS_URL"] and not any(
                shutil.which(name, path=cfg.env["PATH"])
                for name in ("valkey-server", "redis-server")
            ):
                packages.append("valkey")
            if cfg.managed["OLLAMA_BASE_URL"] and not shutil.which(
                "ollama", path=cfg.env["PATH"]
            ):
                packages.append("ollama")
            for package in packages:
                if package in {"git", "curl", "tesseract"} and shutil.which(
                    package, path=cfg.env["PATH"]
                ):
                    continue
                if subprocess.run(
                    [cfg.tool("brew"), "list", "--versions", package],
                    capture_output=True,
                    env=cfg.env,
                ).returncode:
                    run([cfg.tool("brew"), "install", package], cfg)
        elif sys.platform.startswith("linux") and shutil.which("apt-get"):
            self.apt(
                [
                    "python3-venv",
                    "python3-dev",
                    "git",
                    "curl",
                    "ca-certificates",
                    "build-essential",
                    "libffi-dev",
                    "libssl-dev",
                    "libxml2-dev",
                    "libxslt1-dev",
                    "zlib1g-dev",
                    "tesseract-ocr",
                    "tesseract-ocr-eng",
                    "libpango-1.0-0",
                    "libpangoft2-1.0-0",
                    "libharfbuzz-subset0",
                    "fontconfig",
                    "fonts-dejavu-core",
                ]
            )
            if cfg.managed["DATABASE_URL"]:
                if subprocess.run(
                    ["apt-cache", "show", "postgresql-17"], capture_output=True
                ).returncode:
                    self.apt(["postgresql-common"])
                    run(
                        [
                            "sudo",
                            "/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh",
                            "-y",
                        ],
                        cfg,
                    )
                self.apt(
                    [
                        "postgresql-17",
                        "postgresql-client-17",
                        "postgresql-server-dev-17",
                        "postgresql-17-pgvector",
                    ]
                )
            if cfg.managed["REDIS_URL"]:
                # Valkey is preferred; Redis implements the same app protocol.
                package = (
                    "valkey"
                    if subprocess.run(
                        ["apt-cache", "show", "valkey"], capture_output=True
                    ).returncode
                    == 0
                    else "redis-server"
                )
                self.apt([package])
            if cfg.managed["OLLAMA_BASE_URL"] and not shutil.which(
                "ollama", path=cfg.env["PATH"]
            ):
                script = cfg.runtime / "install-ollama.sh"
                download("https://ollama.com/install.sh", script)
                run(["sh", script], cfg)
        elif os.name == "nt":
            # Experimental: native Windows may need pgvector/PDF/Valkey binaries
            # supplied by the user. The wrapper offers WSL on any setup failure.
            for executable, package in [
                ("git", "Git.Git"),
                ("tesseract", "UB-Mannheim.TesseractOCR"),
                ("ollama", "Ollama.Ollama"),
            ]:
                if not shutil.which(executable, path=cfg.env["PATH"]):
                    run(
                        [
                            "winget",
                            "install",
                            "--id",
                            package,
                            "--exact",
                            "--accept-package-agreements",
                            "--accept-source-agreements",
                        ],
                        cfg,
                    )
        else:
            log(
                "No automatic system installer for this distribution. Using dependencies already on PATH.",
                "warning",
            )
        self.node()

    def node(self):
        cfg = self.cfg
        existing = shutil.which("node", path=cfg.env["PATH"])
        if existing:
            version = subprocess.check_output([existing, "--version"], text=True)
            parts = version.strip().lstrip("v").split(".")
            if tuple(int(part) for part in parts[:2]) >= (22, 12):
                return
        # A private official Node distribution avoids replacing an older install.
        system = {"darwin": "darwin", "win32": "win"}.get(sys.platform, "linux")
        arch = {
            "arm64": "arm64",
            "aarch64": "arm64",
            "x86_64": "x64",
            "AMD64": "x64",
        }.get(platform.machine())
        if not arch:
            raise RuntimeError("Unsupported Node architecture; supply Node 22+ on PATH")
        with urllib.request.urlopen(
            "https://nodejs.org/dist/index.json", timeout=30
        ) as response:
            release = next(
                x["version"]
                for x in json.load(response)
                if x["version"].startswith("v22.")
            )
        filename = f"node-{release}-{system}-{arch}." + (
            "zip" if os.name == "nt" else "tar.gz"
        )
        base = f"https://nodejs.org/dist/{release}/"
        archive = cfg.runtime / "downloads" / filename
        download(base + filename, archive)
        with urllib.request.urlopen(base + "SHASUMS256.txt", timeout=30) as response:
            hashes = dict(
                (line.split()[1], line.split()[0])
                for line in response.read().decode().splitlines()
            )
        with archive.open("rb") as downloaded:
            if (
                hashlib.file_digest(downloaded, "sha256").hexdigest()
                != hashes[filename]
            ):
                raise RuntimeError("Node download checksum mismatch")
        staging = cfg.runtime / "downloads/node-unpacked"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()
        if os.name == "nt":
            with zipfile.ZipFile(archive) as source:
                source.extractall(staging)
        else:
            with tarfile.open(archive) as source:
                source.extractall(staging, filter="data")
        destination = cfg.runtime / "tools/node"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise RuntimeError(
                "Private Node directory already exists; refusing to overwrite it"
            )
        next(staging.iterdir()).rename(destination)

    def app(self, dev=False):
        cfg = self.cfg
        if not cfg.python.exists():
            if cfg.custom_python:
                raise RuntimeError("Configured NATIVE_PYTHON does not exist")
            if sys.version_info[:2] == (3, 12):
                venv.EnvBuilder(with_pip=True).create(cfg.python.parent.parent)
            else:
                bootstrap = cfg.runtime / "bootstrap"
                venv.EnvBuilder(with_pip=True).create(bootstrap)
                python = bootstrap / (
                    "Scripts/python.exe" if os.name == "nt" else "bin/python"
                )
                run([python, "-m", "pip", "install", "uv"], cfg)
                run(
                    [
                        python,
                        "-m",
                        "uv",
                        "venv",
                        "--python",
                        "3.12",
                        "--seed",
                        cfg.python.parent.parent,
                    ],
                    cfg,
                    env={
                        **cfg.env,
                        "UV_PYTHON_INSTALL_DIR": str(cfg.runtime / "python"),
                    },
                )
        files = [
            cfg.root / "backend/poetry.lock",
            cfg.root / "backend/pyproject.toml",
            cfg.root / "frontend/package-lock.json",
        ]
        digest = hashlib.sha256(
            b"".join(path.read_bytes() for path in files) + str(cfg.python).encode()
        ).hexdigest()
        stamp = cfg.runtime / "setup.json"
        previous = json.loads(stamp.read_text()) if stamp.exists() else {}
        if (
            previous.get("dependencies") != digest
            or not (cfg.root / "frontend/node_modules/.package-lock.json").exists()
        ):
            run([cfg.python, "-m", "pip", "install", "poetry"], cfg)
            env = {
                **cfg.env,
                "VIRTUAL_ENV": str(cfg.python.parent.parent),
                "POETRY_VIRTUALENVS_CREATE": "false",
            }
            run(
                [
                    cfg.python,
                    "-m",
                    "poetry",
                    "install",
                    "--no-root",
                    "--no-interaction",
                ],
                cfg,
                cfg.root / "backend",
                env,
            )
            run([cfg.tool("npm"), "ci"], cfg, cfg.root / "frontend")
            atomic_json(stamp, {"dependencies": digest})
        if not dev:
            run([cfg.tool("npm"), "run", "build"], cfg, cfg.root / "frontend")

    def clone(self, name, url):
        directory = self.cfg.runtime / "sources" / name
        if not directory.exists():
            directory.parent.mkdir(parents=True, exist_ok=True)
            run(
                [self.cfg.tool("git"), "clone", "--depth", "1", url, directory],
                self.cfg,
            )
        if not (directory / ".git").exists():
            raise RuntimeError(
                f"Refusing to overwrite an existing non-checkout: {directory}"
            )
        origin = subprocess.check_output(
            [self.cfg.tool("git"), "-C", str(directory), "remote", "get-url", "origin"],
            text=True,
        ).strip()
        if origin != url:
            raise RuntimeError(
                f"Unexpected source repository at {directory}; refusing to install it"
            )
        return directory

    def search(self):
        cfg = self.cfg
        if cfg.managed["SEARXNG_BASE_URL"]:
            source = self.clone("searxng", "https://github.com/searxng/searxng.git")
            target = cfg.envs / "searxng"
            python = target / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            if not (target / ".ready").exists():
                run([cfg.python, "-m", "venv", target], cfg)
                run(
                    [
                        python,
                        "-m",
                        "pip",
                        "install",
                        "setuptools",
                        "wheel",
                        "pyyaml",
                        "msgspec",
                        "typing-extensions",
                        "pybind11",
                        "granian",
                    ],
                    cfg,
                )
                run(
                    [
                        python,
                        "-m",
                        "pip",
                        "install",
                        "--use-pep517",
                        "--no-build-isolation",
                        "-e",
                        source,
                    ],
                    cfg,
                )
                pybabel = target / (
                    "Scripts/pybabel.exe" if os.name == "nt" else "bin/pybabel"
                )
                run([pybabel, "compile", "-d", "searx/translations"], cfg, source)
                (target / ".ready").touch()
        if cfg.managed["DEGOOG_BASE_URL"]:
            source = self.clone("degoog", "https://github.com/degoog-org/degoog.git")
            requirement = (
                json.loads((source / "package.json").read_text())
                .get("engines", {})
                .get("bun", "1.4.4")
            )
            match = re.search(r"\d+\.\d+\.\d+", requirement)
            if not match:
                raise RuntimeError(
                    "Unsupported DeGoog Bun requirement; install a compatible Bun manually"
                )
            minimum = tuple(int(x) for x in match[0].split("."))
            existing = shutil.which("bun", path=cfg.env["PATH"])
            version = (
                subprocess.check_output([existing, "--version"], text=True).strip()
                if existing
                else "0.0.0"
            )
            installed = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
            if not installed or tuple(int(x) for x in installed.groups()) < minimum:
                run(
                    [
                        cfg.tool("npm"),
                        "install",
                        "--prefix",
                        cfg.runtime / "tools/bun",
                        "bun@" + match[0],
                    ],
                    cfg,
                )
            if not (source / ".ready").exists():
                run([cfg.tool("bun"), "install", "--frozen-lockfile"], cfg, source)
                run([cfg.tool("bun"), "run", "build"], cfg, source)
                (source / ".ready").touch()

    def vector(self):
        cfg = self.cfg
        if not cfg.managed["DATABASE_URL"]:
            return
        config = cfg.postgres_bin("pg_config")
        shared = Path(
            subprocess.check_output([config, "--sharedir"], text=True).strip()
        )
        if (shared / "extension/vector.control").exists():
            return
        source = self.clone("pgvector", "https://github.com/pgvector/pgvector.git")
        env = {**cfg.env, "PG_CONFIG": config}
        if os.name == "nt":
            env["PGROOT"] = str(Path(config).parent.parent)
            run(["nmake", "/F", "Makefile.win"], cfg, source, env)
            run(["nmake", "/F", "Makefile.win", "install"], cfg, source, env)
        else:
            options = []
            if sys.platform == "darwin":
                # Homebrew's build SDK may differ from the installed Xcode SDK.
                sdk = subprocess.check_output(
                    ["xcrun", "--show-sdk-path"], text=True
                ).strip()
                options.append(f"PG_SYSROOT={sdk}")
            run(["make", *options], cfg, source, env)
            prefix = (
                [] if sys.platform == "darwin" else ["sudo", "--preserve-env=PG_CONFIG"]
            )
            run([*prefix, "make", *options, "install"], cfg, source, env)
