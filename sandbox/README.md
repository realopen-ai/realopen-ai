# Coding sandbox

This directory builds the environment used by the dedicated coder agent. The host runtime creates individual Docker containers from this image and mounts each workspace's persistent named volume at `/workspace`.

## Contents

- [Dockerfile](Dockerfile): Ubuntu-based image with Python, uv, autopep8, Node.js, npm, Bun, Git, SQLite, and shell utilities.
- `helpers/roai-tree`: workspace tree output.
- `helpers/roai-read` / `roai-write`: workspace file access.
- `helpers/roai-project-info`: project inspection.
- `helpers/roai-python-setup`: run `uv sync --all-groups` from `/workspace`, requiring `pyproject.toml`.
- `helpers/roai-node-setup`: run `bun install` from `/workspace`, requiring `package.json`.

Helpers are installed as executable `roai-*` commands on the container's PATH. The default image command keeps the container alive; project servers are started separately.

## Build and use

From the repository root:

```sh
make sandbox-build
```

This builds `realopenai-sandbox:latest`. After changing the image or helpers, rebuild it; already-running containers do not automatically receive the new image.

Use **Workspace → Sandboxes** to create and manage workspaces and their CPU, RAM, and storage configuration. Delegating a coding task also creates a default workspace if none is linked to the conversation.

The right panel exposes files, agent command output, an independent **User shell**, and app previews. The coder loop lives in [backend/app/agent/coder](../backend/app/agent/coder); Docker operations live in [scripts/sandbox_runtime.py](../scripts/sandbox_runtime.py).

## Persistence and previews

- Workspace content is in a Docker-managed named volume, not a host `sandbox/workspace/` folder.
- Stopping/recreating a compute container preserves workspace content; deleting a workspace removes its volume.
- Command history is persisted by the backend in PostgreSQL.
- Project conventions use backend port **6969** and frontend port **6767**. Servers must listen on `0.0.0.0` to be reachable outside the container.
- Check the preview address for the actual host port, particularly with multiple workspaces. Frontend previews take priority when available.

Use uv for Python dependencies and Bun for JavaScript dependencies. Installed dependencies and project files stay in the workspace volume.

## Safety

Containers separate coding workloads from the application, but are not a guarantee that arbitrary code is safe. Review generated code, imported skill scripts, and network access. Do not put secrets in workspaces unless the task explicitly requires them.
