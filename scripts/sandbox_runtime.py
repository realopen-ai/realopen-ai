"""Host-side Docker control plane for persistent RealOpen sandboxes.

This module is imported only by the loopback host runtime. The application
container never receives the Docker socket.
"""

from __future__ import annotations

import io
import json
import posixpath
import re
import shlex
import tarfile
from dataclasses import dataclass

try:
    import docker
    from docker.errors import NotFound
except ImportError:  # reported through /health rather than breaking voice
    docker = None
    NotFound = Exception


LABEL = "ai.realopen.sandbox"
WORKSPACE = "/workspace"
FRONTEND_PORT = 6767
BACKEND_PORT = 6969
PREVIEW_PORTS = (FRONTEND_PORT, BACKEND_PORT)


def safe_path(path: str) -> str:
    value = path or ""
    # Coder models commonly return repository-relative paths (for example
    # ``tests/test_api.py``), while the file explorer returns absolute
    # ``/workspace/...`` paths. Support both without weakening containment.
    raw = value if value.startswith("/") else posixpath.join(WORKSPACE, value)
    normalized = posixpath.normpath(raw)
    if normalized == WORKSPACE or normalized.startswith(WORKSPACE + "/"):
        return normalized
    raise ValueError("Path must stay inside /workspace")


@dataclass
class SandboxSpec:
    sandbox_id: str
    volume_name: str
    container_name: str
    image: str
    cpu_limit: float
    memory_limit_mb: int


class SandboxRuntime:
    def __init__(self):
        if docker is None:
            raise RuntimeError("Docker SDK missing; run `make sandbox-install`")
        self.client = docker.from_env()

    def ping(self) -> bool:
        return bool(self.client.ping())

    def _container(self, name: str):
        try:
            return self.client.containers.get(name)
        except NotFound:
            return None

    def create(self, spec: SandboxSpec) -> dict:
        try:
            self.client.volumes.get(spec.volume_name)
        except NotFound:
            self.client.volumes.create(
                spec.volume_name, labels={LABEL: spec.sandbox_id}
            )
        self._ensure_workspace_owner(spec)
        return self.start(spec)

    def _ensure_workspace_owner(self, spec: SandboxSpec) -> None:
        """Keep the persistent volume writable without running the main container as root."""
        self.client.containers.run(
            spec.image,
            ["chown", "-R", "1000:1000", WORKSPACE],
            remove=True,
            volumes={spec.volume_name: {"bind": WORKSPACE, "mode": "rw"}},
        )

    def start(self, spec: SandboxSpec) -> dict:
        try:
            self.client.volumes.get(spec.volume_name)
        except NotFound as exc:
            raise RuntimeError(
                "Persistent workspace volume is missing; refusing to create a blank replacement"
            ) from exc
        # This also repairs volumes created before ownership initialization
        # was introduced. The short-lived helper has no application data or
        # Docker socket; the long-lived container remains UID 1000/cap-drop.
        self._ensure_workspace_owner(spec)
        container = self._container(spec.container_name)
        if container is not None:
            container.reload()
            bindings = container.attrs.get("HostConfig", {}).get("PortBindings") or {}
            current_image = self.client.images.get(spec.image)
            if container.attrs.get("Image") != current_image.id or not all(
                f"{port}/tcp" in bindings for port in PREVIEW_PORTS
            ):
                # Port bindings cannot be changed in-place. Recreate only the
                # disposable compute container; the workspace volume survives.
                if container.status == "running":
                    container.stop(timeout=5)
                container.remove(force=True)
                container = None
        if container is None:
            container = self.client.containers.create(
                spec.image,
                name=spec.container_name,
                command=["sleep", "infinity"],
                detach=True,
                working_dir=WORKSPACE,
                labels={LABEL: spec.sandbox_id},
                volumes={spec.volume_name: {"bind": WORKSPACE, "mode": "rw"}},
                nano_cpus=max(1, int(spec.cpu_limit * 1_000_000_000)),
                mem_limit=f"{spec.memory_limit_mb}m",
                user="1000:1000",
                pids_limit=256,
                security_opt=["no-new-privileges:true"],
                cap_drop=["ALL"],
                ports={f"{port}/tcp": ("127.0.0.1", None) for port in PREVIEW_PORTS},
            )
        else:
            container.reload()
        if container.status != "running":
            container.start()
            container.reload()
        return self.status(spec)

    def stop(self, spec: SandboxSpec) -> dict:
        container = self._container(spec.container_name)
        if container is not None:
            container.reload()
            if container.status == "running":
                container.stop(timeout=5)
            container.remove(force=True)
        return {"status": "stopped", "running": False}

    def restart(self, spec: SandboxSpec) -> dict:
        self.stop(spec)
        return self.start(spec)

    def delete(self, spec: SandboxSpec) -> None:
        self.stop(spec)
        try:
            self.client.volumes.get(spec.volume_name).remove(force=True)
        except NotFound:
            pass

    def status(self, spec: SandboxSpec) -> dict:
        container = self._container(spec.container_name)
        running = bool(container and container.status == "running")
        return {"status": "running" if running else "stopped", "running": running}

    def exec(
        self,
        spec: SandboxSpec,
        command: str,
        timeout: int = 120,
        command_id: str = "command",
    ) -> dict:
        container = self._container(spec.container_name)
        if not container or container.status != "running":
            raise RuntimeError("Sandbox is not running")
        # Docker SDK waits for completion. The command itself is bounded by
        # coreutils timeout inside the sandbox image.
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", command_id)[:80] or "command"
        script = (
            f"echo $$ > /tmp/roai-{safe_id}.pid; "
            f"trap 'rm -f /tmp/roai-{safe_id}.pid' EXIT; "
            f"exec timeout --signal=TERM {max(1, min(timeout, 1800))} "
            f"bash -lc {shlex.quote(command)}"
        )
        wrapped = ["bash", "-lc", script]
        result = container.exec_run(wrapped, workdir=WORKSPACE, demux=True)
        stdout, stderr = result.output or (b"", b"")
        return {
            "command_id": safe_id,
            "exit_code": result.exit_code,
            "stdout": (stdout or b"").decode("utf-8", "replace"),
            "stderr": (stderr or b"").decode("utf-8", "replace"),
        }

    def exec_detached(
        self, spec: SandboxSpec, command: str, command_id: str = "preview"
    ) -> dict:
        container = self._container(spec.container_name)
        if not container or container.status != "running":
            raise RuntimeError("Sandbox is not running")
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", command_id)[:80] or "preview"
        wrapped = (
            f"mkdir -p /tmp/realopen-preview; "
            f"nohup bash -lc {shlex.quote(command)} "
            f">/tmp/realopen-preview/{safe_id}.log 2>&1 "
            "</dev/null &"
        )
        container.exec_run(["bash", "-lc", wrapped], detach=True, workdir=WORKSPACE)
        return {"command_id": safe_id, "started": True}

    def preview_target(self, spec: SandboxSpec, port: int) -> str:
        if port not in PREVIEW_PORTS:
            raise ValueError(f"Preview port must be one of {PREVIEW_PORTS}")
        container = self._container(spec.container_name)
        if not container or container.status != "running":
            raise RuntimeError("Sandbox is not running")
        container.reload()
        bindings = (
            container.attrs.get("NetworkSettings", {})
            .get("Ports", {})
            .get(f"{port}/tcp")
        )
        if not bindings:
            raise RuntimeError("Preview port is unavailable; restart the sandbox")
        return f"http://127.0.0.1:{bindings[0]['HostPort']}"

    def cancel(self, spec: SandboxSpec, command_id: str) -> bool:
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", command_id)[:80]
        if not safe_id:
            raise ValueError("Invalid command id")
        result = self.exec(
            spec,
            f"test -f /tmp/roai-{safe_id}.pid && kill -TERM $(cat /tmp/roai-{safe_id}.pid)",
            timeout=10,
            command_id=f"cancel-{safe_id}",
        )
        return result["exit_code"] == 0

    def usage(self, spec: SandboxSpec) -> int:
        result = self.exec(spec, "du -sb /workspace | cut -f1", timeout=20)
        try:
            return int(result["stdout"].strip())
        except ValueError:
            return 0

    def list_files(
        self, spec: SandboxSpec, path: str = WORKSPACE, depth: int = 4
    ) -> list[dict]:
        target = safe_path(path)
        script = (
            "roai-tree" if target == WORKSPACE else f"roai-tree {json.dumps(target)}"
        )
        result = self.exec(
            spec, f"{script} --depth {max(1, min(depth, 8))}", timeout=30
        )
        if result["exit_code"]:
            raise RuntimeError(result["stderr"] or "Could not list files")
        return json.loads(result["stdout"] or "[]")

    def read_file(
        self, spec: SandboxSpec, path: str, max_bytes: int = 1_000_000
    ) -> bytes:
        target = safe_path(path)
        container = self._container(spec.container_name)
        stream, _ = container.get_archive(target)
        payload = b"".join(stream)
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            member = archive.getmembers()[0]
            if member.size > max_bytes:
                raise ValueError("File is too large to open")
            extracted = archive.extractfile(member)
            return extracted.read() if extracted else b""

    def write_file(self, spec: SandboxSpec, path: str, data: bytes) -> None:
        target = safe_path(path)
        parent, name = posixpath.split(target)
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        info.mode = 0o644
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as archive:
            archive.addfile(info, io.BytesIO(data))
        buf.seek(0)
        container = self._container(spec.container_name)
        mkdir = container.exec_run(["mkdir", "-p", parent], demux=True)
        if mkdir.exit_code:
            _stdout, stderr = mkdir.output or (b"", b"")
            raise RuntimeError(
                f"Could not create parent directory {parent}: "
                f"{(stderr or b'permission denied').decode('utf-8', 'replace').strip()}"
            )
        if not container.put_archive(parent, buf.read()):
            raise RuntimeError("Could not write file")

    def pty_socket(self, spec: SandboxSpec, cols: int, rows: int):
        container = self._container(spec.container_name)
        if not container or container.status != "running":
            raise RuntimeError("Sandbox is not running")
        api = self.client.api
        exec_id = api.exec_create(
            container.id, ["bash", "-l"], stdin=True, tty=True, workdir=WORKSPACE
        )["Id"]
        sock = api.exec_start(exec_id, detach=False, tty=True, socket=True)
        api.exec_resize(exec_id, height=rows, width=cols)
        return sock, exec_id
