# Host and setup scripts

These scripts connect the containerized application to native host services and handle setup, hardware detection, model installation, and health checks. See the [project README](../README.md) for the complete startup flow.

## Important files

| File | Purpose |
| --- | --- |
| `setup.sh` | First-time hardware detection, configuration, Ollama setup, and model downloads |
| `detect-hardware.sh` | Write detected hardware information to `data/hardware.json` |
| `startup.sh` | Create missing configuration, start Ollama/host runtime, and ensure the sandbox image exists |
| `profile-helper.py` | Query and validate the root model/module YAML files |
| `install-voice-models.py` | Delegate speech dependency/model installation to the backend installer |
| `host-runtime-server.py` | Native FastAPI runtime for ASR/TTS, custom voices, and sandbox operations |
| `sandbox_runtime.py` | Docker containers, persistent workspace volumes, file operations, execution, and previews |
| `health-check.sh` | Check application service health |
| `init.sql` | Initial PostgreSQL configuration, mounted by Compose |

## Usage

Run these commands from the repository root:

```sh
make setup
make detect-hardware
make startup
make voice-install
make health
make validate-profiles
make validate-modules
```

For foreground host-runtime debugging:

```sh
cd backend
poetry install --no-root --with dev
poetry run python ../scripts/host-runtime-server.py
```

Startup normally manages this process. Stop any existing instance before starting another on the same port. The runtime binds to `127.0.0.1:8766`; the backend reaches it through the configured host gateway. Startup writes its log and PID under `data/host-runtime.log` and `data/host-runtime.pid`.

## Configuration and safety

Model choices come from [profiles.yml](../profiles.yml); optional modules come from [modules.yml](../modules.yml). Runtime settings are described in [.env.example](../.env.example).

Speech processing runs on the host to use native acceleration, including the Apple Silicon MLX path. Model downloads and dependency installation require network access; cached speech processing does not require a cloud LLM API.

The runtime has Docker-management capabilities. Keep it private and do not expose its port as a public API. Workspace deletion removes the corresponding persistent volume.
