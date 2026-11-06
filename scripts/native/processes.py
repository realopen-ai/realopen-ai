"""Own only processes we launch; never kill arbitrary port owners or stale PIDs."""

import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.request

from .output import log


def port_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_http(url, process=None, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process and process.poll() is not None:
            raise RuntimeError(
                f"Service exited during startup. See data/logs/native/. Endpoint: {url}"
            )
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, ValueError):
            pass
        time.sleep(0.25)
    raise RuntimeError(
        f"Service not ready after {timeout}s: {url}. See data/logs/native/."
    )


class Processes:
    def __init__(self, data, groups=True):
        self.logs = Path(data) / "logs/native"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.owned = {}
        self.groups = groups

    def start(self, name, command, cwd, env, port=None):
        current = self.owned.get(name)
        if current and current.poll() is None:
            return current
        if port and port_open("127.0.0.1", port):
            raise RuntimeError(
                f"Port {port} is already occupied; refusing to adopt/stop it. Configure an external endpoint or another managed port."
            )
        log(f"{name} — log: {self.logs / (name + '.log')}", "start")
        with (self.logs / (name + ".log")).open("ab") as output:
            process = subprocess.Popen(
                [str(x) for x in command],
                cwd=cwd,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=self.groups and os.name != "nt",
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if self.groups and os.name == "nt"
                    else 0
                ),
            )
        self.owned[name] = process
        return process

    def stop(self, name):
        process = self.owned.pop(name, None)
        if not process or process.poll() is not None:
            return
        try:
            if self.groups and os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            elif os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.terminate()
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            if self.groups and os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=10)

    def close(self):
        for name in reversed(list(self.owned)):
            try:
                self.stop(name)
            except OSError as exc:
                log(f"Could not stop managed process {name}: {exc}", "warning")


class RuntimeLock:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.handle.seek(0, os.SEEK_END) == 0:
            self.handle.write(b"\0")
        self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError(
                "A native launcher/migration is already running for this data directory"
            )
        return self

    def __exit__(self, *_):
        self.handle.close()
