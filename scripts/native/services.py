"""Managed native PostgreSQL/Valkey and on-demand search processes."""

import os
import re
import secrets
import shutil
import subprocess
import time
import threading
from urllib.parse import urlsplit

from .config import Config
from .processes import Processes, port_open, wait_http


class Services:
    def __init__(self, cfg, processes):
        self.cfg, self.processes = cfg, processes

    def psql(self, sql, database=None):
        env = self.cfg.pg_env()
        if database:
            env["PGDATABASE"] = database
        return subprocess.check_output(
            [
                self.cfg.postgres_bin("psql"),
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-At",
                "-c",
                sql,
            ],
            env=env,
            text=True,
        ).strip()

    def database(self):
        cfg = self.cfg
        if not cfg.managed["DATABASE_URL"]:
            return
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            raise RuntimeError(
                "Run the native launcher as your regular user, not root. Only dependency installation uses sudo."
            )
        directory = cfg.data / "postgres/native"
        directory.parent.mkdir(parents=True, exist_ok=True)
        env = cfg.pg_env()
        if not (directory / "PG_VERSION").exists():
            if directory.exists() and any(directory.iterdir()):
                raise RuntimeError(
                    "Native PostgreSQL directory is not empty and has no PG_VERSION; refusing to overwrite"
                )
            password_file = cfg.runtime / "postgres-password.tmp"
            password_file.write_text(env["PGPASSWORD"], encoding="utf-8")
            password_file.chmod(0o600)
            try:
                subprocess.run(
                    [
                        cfg.postgres_bin("initdb"),
                        "-D",
                        str(directory),
                        "-U",
                        env["PGUSER"],
                        "--pwfile",
                        str(password_file),
                        "--auth=scram-sha-256",
                        "--encoding=UTF8",
                        "--locale=C",
                    ],
                    env=cfg.env,
                    check=True,
                )
            finally:
                password_file.unlink(missing_ok=True)
        version = subprocess.check_output(
            [cfg.postgres_bin("postgres"), "--version"], text=True
        )
        match = re.search(r"PostgreSQL\)\s+(\d+)", version)
        if not match:
            raise RuntimeError(
                "Cannot determine the installed PostgreSQL major version"
            )
        major = match[1]
        if (directory / "PG_VERSION").read_text().strip() != major:
            raise RuntimeError(
                "Native PostgreSQL major version changed; use pg_dump/restore, never open an incompatible cluster"
            )
        if (directory / "postmaster.pid").exists():
            raise RuntimeError(
                "Native database has a postmaster.pid. Check for an existing server before recovering a stale lock; it will not be deleted automatically."
            )
        command = [
            cfg.postgres_bin("postgres"),
            "-D",
            directory,
            "-h",
            "127.0.0.1",
            "-p",
            env["PGPORT"],
        ]
        if os.name != "nt":
            # Clients use loopback TCP. Avoid OS Unix-socket path length limits
            # for checkouts in long directories (including temporary tests).
            command += ["-k", ""]
        self.processes.start(
            "postgres",
            command,
            cfg.root,
            {**cfg.env, "LC_ALL": "C"},
            int(env["PGPORT"]),
        )
        deadline = time.monotonic() + 30
        while subprocess.run(
            [cfg.postgres_bin("pg_isready"), "-q"],
            env={**env, "PGDATABASE": "postgres"},
        ).returncode:
            if time.monotonic() > deadline:
                raise RuntimeError(
                    "Native PostgreSQL did not become ready; see data/logs/native/postgres.log"
                )
            time.sleep(0.25)
        quoted = '"' + env["PGDATABASE"].replace('"', '""') + '"'
        exists = self.psql("SELECT datname FROM pg_database", "postgres").splitlines()
        if env["PGDATABASE"] not in exists:
            self.psql(f"CREATE DATABASE {quoted}", "postgres")
        self.psql("CREATE EXTENSION IF NOT EXISTS vector")

    def valkey(self):
        cfg = self.cfg
        if not cfg.managed["REDIS_URL"]:
            return
        endpoint = urlsplit(cfg.env["REDIS_URL"])
        directory = cfg.data / "valkey/native"
        directory.mkdir(parents=True, exist_ok=True)
        executable = shutil.which("valkey-server", path=cfg.env["PATH"]) or cfg.tool(
            "redis-server"
        )
        port = endpoint.port or 6379
        if endpoint.password:
            raise RuntimeError(
                "Use an external Redis/Valkey instance for authenticated URLs"
            )
        self.processes.start(
            "valkey",
            [
                executable,
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
                "--dir",
                directory,
                "--appendonly",
                "yes",
                "--save",
                "60",
                "1",
            ],
            cfg.root,
            cfg.env,
            port,
        )
        deadline = time.monotonic() + 15
        while not port_open("127.0.0.1", port):
            if time.monotonic() > deadline:
                raise RuntimeError("Valkey did not become ready")
            time.sleep(0.1)


class NativeSearchRuntime:
    """Same host-runtime API as Docker; only own configured native instances."""

    def __init__(self, root=None):
        self.cfg = Config() if root is None else Config(root)
        self.processes = Processes(self.cfg.data, groups=False)
        self.lock = threading.Lock()

    def reconcile(self, desired):
        with self.lock:
            return self._reconcile(desired)

    def _reconcile(self, desired):
        if set(desired) != {"searxng", "degoog"} or any(
            type(x) is not bool for x in desired.values()
        ):
            raise ValueError("Expected boolean searxng/degoog choices")
        states = {}
        for name, enabled in desired.items():
            try:
                key = name.upper() + "_BASE_URL"
                if not self.cfg.managed[key]:
                    states[name] = {"status": "external" if enabled else "stopped"}
                    continue
                if not enabled:
                    self.processes.stop(name)
                    states[name] = {"status": "stopped"}
                    continue
                cfg = self.cfg
                port = urlsplit(cfg.env[key]).port
                env = dict(cfg.env)
                source = cfg.runtime / "sources" / name
                if name == "searxng":
                    settings = cfg.data / "searxng/settings.yml"
                    if not settings.exists():
                        settings.parent.mkdir(parents=True, exist_ok=True)
                        contents = (
                            cfg.root / "monitoring/searxng-settings.yml"
                        ).read_text()
                        contents = (
                            contents.replace(
                                "realopen-searxng-secret-change-me",
                                secrets.token_hex(32),
                            )
                            .replace("0.0.0.0", "127.0.0.1")
                            .replace("valkey://redis:6379/0", cfg.env["REDIS_URL"])
                        )
                        settings.write_text(contents, encoding="utf-8")
                    env["SEARXNG_SETTINGS_PATH"] = str(settings)
                    env["SEARXNG_VALKEY_URL"] = cfg.env["REDIS_URL"]
                    limiter = settings.with_name("limiter.toml")
                    if not limiter.exists():
                        shutil.copyfile(
                            cfg.root / "monitoring/searxng-limiter.toml", limiter
                        )
                    python = (
                        cfg.envs
                        / "searxng"
                        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                    )
                    command = [
                        python,
                        "-m",
                        "granian",
                        "--interface",
                        "wsgi",
                        "--workers",
                        "1",
                        "--blocking-threads",
                        "4",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "searx.webapp:app",
                    ]
                    health = "/healthz"
                else:
                    env.update(
                        DEGOOG_DATA_DIR=str(cfg.data / "degoog"),
                        DEGOOG_BIND_ADDRESS="127.0.0.1",
                        DEGOOG_PORT=str(port),
                        DEGOOG_VALKEY_URL=cfg.env["REDIS_URL"],
                        DEGOOG_PYTHON_BIN=str(cfg.python),
                    )
                    command = [cfg.tool("bun"), "run", "start"]
                    health = "/readyz"
                process = self.processes.start(name, command, source, env, port)
                wait_http(cfg.env[key].rstrip("/") + health, process, timeout=45)
                states[name] = {"status": "ready"}
            except (
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
                ValueError,
            ) as exc:
                states[name] = {"status": "failed", "error": str(exc)}
        return states

    def close(self):
        with self.lock:
            self.processes.close()
