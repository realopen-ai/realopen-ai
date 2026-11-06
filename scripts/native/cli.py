"""Foreground native supervisor with an explicit setup/migration lifecycle."""

import argparse
import json
import os
import signal
import threading
import urllib.request
import uuid
from urllib.parse import urlsplit

from .config import Config, atomic_json
from .dependencies import Installer, run
from .migration import check_legacy, restore_native
from .processes import Processes, RuntimeLock, wait_http
from .output import log
from .services import Services


def setup(cfg, dev):
    installer = Installer(cfg)
    installer.system()
    installer.app(dev)
    installer.vector()
    installer.search()
    if os.name != "nt":
        run(["bash", cfg.root / "scripts/detect-hardware.sh", cfg.data], cfg)
    else:
        log(
            "Experimental Windows native mode. Configure HARDWARE_PROFILE in .env.native if needed.",
            "warning",
        )


def launch(cfg, dev=False, install=True, migrate=False):
    with RuntimeLock(cfg.runtime / "runner.lock"):
        check_legacy(cfg.root)
        if (
            not migrate
            and cfg.managed["DATABASE_URL"]
            and (cfg.data / "postgres/docker/PG_VERSION").exists()
            and not (cfg.runtime / "migration.json").exists()
        ):
            raise RuntimeError(
                "Docker database files exist. Run scripts/migrate-storage.py to export "
                "the database, then start-native migrate before starting native mode."
            )
        if install:
            setup(cfg, dev)
        processes = Processes(cfg.data)
        stop = threading.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop.set())
        session = uuid.uuid4().hex
        try:
            services = Services(cfg, processes)
            if migrate:
                restore_native(cfg, services)
                return
            services.database()
            services.valkey()
            if cfg.managed["OLLAMA_BASE_URL"]:
                try:
                    with urllib.request.urlopen(
                        cfg.env["OLLAMA_BASE_URL"] + "/api/version", timeout=2
                    ) as response:
                        if "version" not in json.load(response):
                            raise RuntimeError("Endpoint is not Ollama")
                except OSError:
                    port = urlsplit(cfg.env["OLLAMA_BASE_URL"]).port or 11434
                    processes.start(
                        "ollama",
                        [cfg.tool("ollama"), "serve"],
                        cfg.root,
                        {**cfg.env, "OLLAMA_HOST": f"127.0.0.1:{port}"},
                        port,
                    )
                    wait_http(
                        cfg.env["OLLAMA_BASE_URL"] + "/api/version",
                        processes.owned["ollama"],
                    )
            if cfg.managed["VOICE_RUNTIME_URL"]:
                port = urlsplit(cfg.env["VOICE_RUNTIME_URL"]).port or 8766
                process = processes.start(
                    "host-runtime",
                    [cfg.python, cfg.root / "scripts/host-runtime-server.py"],
                    cfg.root,
                    {**cfg.env, "HOST_RUNTIME_PORT": str(port)},
                    port,
                )
                wait_http(cfg.env["VOICE_RUNTIME_URL"] + "/health", process)
            # Extension availability is checked even for external PostgreSQL;
            # use that server's credentials, never provision it via local OS tools.
            script = "import asyncio,os,asyncpg\nasync def main():\n c=await asyncpg.connect(os.environ['DATABASE_URL'].replace('postgresql+asyncpg:', 'postgresql:'))\n try:\n  await c.execute('CREATE EXTENSION IF NOT EXISTS vector')\n finally:\n  await c.close()\nasyncio.run(main())"
            run([cfg.python, "-c", script], cfg)
            run(
                [cfg.python, "-m", "alembic", "upgrade", "head"],
                cfg,
                cfg.root / "backend",
            )
            port = int(cfg.env.get("NATIVE_API_PORT", "8000"))
            command = [
                cfg.python,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--ws-ping-interval",
                "3600",
                "--ws-ping-timeout",
                "3600",
            ]
            env = {
                **cfg.env,
                "STATIC_FRONTEND_DIR": "" if dev else str(cfg.root / "frontend/dist"),
            }
            if not dev and not (cfg.root / "frontend/dist/index.html").exists():
                raise RuntimeError(
                    "Frontend build missing; run start-native setup before --no-install"
                )
            if dev:
                command += ["--reload", "--reload-dir", str(cfg.root / "backend/app")]
            backend = processes.start(
                "backend", command, cfg.root / "backend", env, port
            )
            wait_http(f"http://127.0.0.1:{port}/api/health", backend)
            address = f"http://127.0.0.1:{port}"
            if dev:
                frontend_port = int(cfg.env.get("NATIVE_FRONTEND_PORT", "5173"))
                process = processes.start(
                    "frontend",
                    [
                        cfg.tool("npm"),
                        "run",
                        "dev",
                        "--",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(frontend_port),
                        "--strictPort",
                    ],
                    cfg.root / "frontend",
                    cfg.env,
                    frontend_port,
                )
                address = f"http://127.0.0.1:{frontend_port}"
                wait_http(address, process)
            atomic_json(
                cfg.runtime / "run.json",
                {
                    "pid": os.getpid(),
                    "session": session,
                    "url": address,
                    "mode": "dev" if dev else "normal",
                },
            )
            log(
                f"RealOpen-AI ready: {address}\nPress Ctrl+C to stop. Logs: {processes.logs}",
                "ready",
            )
            while not stop.wait(0.5):
                request = cfg.runtime / "stop.json"
                if (
                    request.exists()
                    and json.loads(request.read_text()).get("session") == session
                ):
                    break
                exited = [
                    name
                    for name, child in processes.owned.items()
                    if child.poll() is not None
                ]
                if exited:
                    raise RuntimeError("Managed process exited: " + ", ".join(exited))
        finally:
            processes.close()
            (cfg.runtime / "run.json").unlink(missing_ok=True)
            (cfg.runtime / "stop.json").unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Run RealOpen-AI directly on the host")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "setup", "doctor", "status", "stop", "migrate"],
    )
    parser.add_argument("--dev", action="store_true")
    parser.add_argument(
        "--no-install",
        action="store_true",
        help="Use existing dependencies without installing/building",
    )
    args = parser.parse_args()
    cfg = Config()
    if args.command in {"status", "stop"}:
        path = cfg.runtime / "run.json"
        if not path.exists():
            log("Native runtime is not running")
            return
        state = json.loads(path.read_text())
        try:
            with RuntimeLock(cfg.runtime / "runner.lock"):
                log("Native runtime is not running (stale status file)")
                return
        except RuntimeError:
            pass  # Only the active supervisor may consume a stop request.
        if args.command == "stop":
            atomic_json(cfg.runtime / "stop.json", {"session": state["session"]})
            log("Requested graceful native shutdown", "success")
        else:
            print(json.dumps(state, indent=2))
    elif args.command == "doctor":
        log(f"Data: {cfg.data}")
        installed = cfg.python.exists()
        log(
            f"Python: {cfg.python} — {'installed' if installed else 'not installed'}",
            "info" if installed else "warning",
        )
        for key, managed in cfg.managed.items():
            # Do not print credentials or full connection URLs.
            log(f"{key}: {'managed' if managed else 'external'}")
        check_legacy(cfg.root)
    elif args.command == "setup":
        with RuntimeLock(cfg.runtime / "runner.lock"):
            setup(cfg, args.dev)
    else:
        launch(cfg, args.dev, not args.no_install, args.command == "migrate")
