"""Allowlisted host-side lifecycle for the bundled search services."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import threading

ROOT = Path(__file__).resolve().parent.parent
SERVICES = {
    "searxng": "docker.io/searxng/searxng:latest",
    "degoog": "ghcr.io/degoog-org/degoog:latest",
}


class SearchRuntime:
    def __init__(self, root: Path = ROOT):
        self.root = root.resolve()
        self.lock = threading.Lock()

    def _run(self, args: list[str], timeout: int = 90):
        return subprocess.run(
            ["docker", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )

    def _compose(self, *args: str):
        return self._run(
            [
                "compose",
                "--project-directory",
                str(self.root),
                "-f",
                str(self.root / "docker-compose.yml"),
                *args,
            ]
        )

    def _inspect(self, provider: str):
        try:
            result = self._run(["container", "inspect", f"realopen-{provider}"])
        except subprocess.CalledProcessError as exc:
            if "No such container" in exc.stderr or "No such object" in exc.stderr:
                return None
            raise
        container = json.loads(result.stdout)[0]
        labels = container.get("Config", {}).get("Labels", {}) or {}
        # Do not adopt containers merely because their names happen to match.
        if labels.get("com.docker.compose.service") != provider or labels.get(
            "com.docker.compose.project.working_dir"
        ) != str(self.root):
            raise ValueError("Container name belongs to a different installation")
        return container

    def _service(self, provider: str, enabled: bool) -> dict:
        container = self._inspect(provider)
        if enabled:
            policy = (
                (container or {})
                .get("HostConfig", {})
                .get("RestartPolicy", {})
                .get("Name")
            )
            if not container or not container["State"].get("Running") or policy != "no":
                # Pulls belong to explicit setup/startup preparation, never inference.
                self._run(["image", "inspect", SERVICES[provider]])
                self._compose(
                    "up", "-d", "--no-deps", "--no-build", "--pull", "never", provider
                )
                container = self._inspect(provider)
        elif container:
            if (
                container.get("HostConfig", {}).get("RestartPolicy", {}).get("Name")
                != "no"
            ):
                self._run(["update", "--restart=no", f"realopen-{provider}"])
            if container["State"].get("Running"):
                self._run(["stop", "--time", "10", f"realopen-{provider}"])
            return {"status": "stopped"}
        if not container:
            return {"status": "stopped"}
        state = container["State"]
        health = state.get("Health", {}).get("Status")
        if health == "unhealthy" or state.get("Status") in {"dead", "exited"}:
            return {"status": "failed", "error": "Container failed its readiness check"}
        return {
            "status": (
                "ready" if state.get("Running") and health != "starting" else "starting"
            )
        }

    def reconcile(self, desired: dict[str, bool]) -> dict:
        if set(desired) != set(SERVICES) or any(
            type(v) is not bool for v in desired.values()
        ):
            raise ValueError("Expected boolean choices for searxng and degoog")
        result = {}
        with self.lock:
            for provider, enabled in desired.items():
                try:
                    result[provider] = self._service(provider, enabled)
                except Exception as exc:
                    # Command output may contain environment values: never return it.
                    reason = (
                        str(exc)
                        if isinstance(exc, ValueError)
                        else (
                            "Docker operation failed. Check Docker and run make startup "
                            "to prepare search images, then retry."
                        )
                    )
                    result[provider] = {"status": "failed", "error": reason}
        return result

    def prepare(self):
        """Cache both images without creating or starting either container."""
        for provider, image in SERVICES.items():
            try:
                self._run(["image", "inspect", image])
            except subprocess.CalledProcessError:
                self._run(["pull", image], timeout=600)
            print(f"Search image ready: {provider}", flush=True)


if __name__ == "__main__":
    SearchRuntime().prepare()
