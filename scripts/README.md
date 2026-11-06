# Host and setup scripts

These scripts run the app natively or connect the containerized application to host services, and handle setup, hardware detection, model installation, and health checks. See the [project README](../README.md) for the complete startup flow.

## Native application startup

From the repository root on macOS or Linux:

```sh
bash scripts/start-native.sh          # install missing dependencies, build, run
bash scripts/start-native.sh --dev    # backend reload + Vite hot reload
bash scripts/start-native.sh setup    # prepare dependencies without running
bash scripts/start-native.sh doctor
bash scripts/start-native.sh status
bash scripts/start-native.sh stop
```

Windows: run `./scripts/start-native.ps1` in PowerShell (`-Dev` for development). It uses an installed WSL distribution by default. Ubuntu/Debian is the supported automatic-install path; keep the checkout in WSL's Linux filesystem so PostgreSQL can enforce its file permissions. `-ExperimentalNative` bypasses WSL. Direct Windows requires available PostgreSQL/pgvector, Redis-compatible and PDF libraries; setup attempts Python/Git/Ollama/Tesseract installation and directs you to WSL if native setup fails. Other Linux distributions can use dependencies already installed on `PATH`.

First setup needs network access and may request administrator access through Homebrew, sudo or winget. It creates an isolated Python environment, installs locked backend/frontend dependencies, and prepares native search sources. Installed service binaries are reused; the launcher does not adopt an existing database cluster. OS package managers may install/update required shared-library dependencies. Normal runs serve the built frontend and API at `http://127.0.0.1:8000`; dev runs serve the frontend at `http://127.0.0.1:5173`. `--no-install` skips dependency preparation/builds after setup. Docker is optional until you use a coder sandbox (`make sandbox-build` prepares its image).

The launcher stays in the foreground. Ctrl+C or `stop` shuts down only its own processes; an existing Ollama server is reused and left running. Logs are in `data/logs/native/`. A lock prevents two launchers/migrations from owning the same data directory.

### Managed and external services

Copy [.env.native.example](../.env.native.example) to `.env.native` only if you need overrides. Precedence is process environment, `.env.native`, then `.env`. The existing Docker-default URLs are translated to local native endpoints without changing `.env`.

- Managed PostgreSQL/pgvector uses a private cluster at `data/postgres/native/`, port **55432**, and a generated password in `data/native/credentials.json`.
- Managed Valkey uses `data/valkey/native/`, port **56379**. Neither service uses your system's default database directory.
- Custom database, Redis, Ollama, SearXNG, DeGoog or host-runtime URLs are external: the launcher neither installs nor starts/stops those services. Use `NATIVE_<SERVICE>_MODE=external` to explicitly treat a default endpoint as external; see the example file for exact names.
- Native SearXNG and DeGoog are prepared during setup but run only when enabled in Setup or Brain → Tools → Web Search. DeGoog still needs engines installed through its setup wizard. Custom search instances remain externally managed.
- Source checkouts and platform-specific environments live under `data/native/`. Existing source checkouts are not automatically updated; deliberately review upstream updates before replacing their prepared environments.

Core setup installs Tesseract with English and PDF rendering libraries. Settings → Dependencies uses the platform package manager for supported optional tools. Native macOS LibreOffice installs into `data/native/apps/`; existing system installations are detected and reused. If administrator access is needed, the UI shows a terminal command rather than requesting your password. Docker's APT recovery is never replayed on native startup. Optional monitoring remains available through the existing Compose monitoring commands.

### Docker storage and one-time migration

New Docker storage uses root `data/` bind mounts, including uploads, module state, caches, monitoring data and sandbox workspaces. PostgreSQL physical storage remains separate: Docker uses `data/postgres/docker/`; native uses `data/postgres/native/`. PostgreSQL data directories cannot be shared between operating systems/builds, and these two databases do not synchronize after migration. Documents, generated assets and other application files remain in shared `data/`.

For an existing named-volume installation, **before recreating its containers**:

```sh
python3 scripts/migrate-storage.py    # or: make migrate-storage
bash scripts/start-native.sh migrate # or: make migrate-native
bash scripts/start-native.sh
```

The first command stops this installation's writers, exports PostgreSQL as a portable dump, stops its database and sandbox containers, and copies legacy volumes into `data/`. It also discovers Compose volumes left by `compose down`. Original volumes and backup archives are retained. Nonempty destinations are refused. The second command restores the dump into an empty managed native database, with checksum verification and a single transaction. External databases are never restored over. If Docker already uses bind mounts, run the first command to produce a fresh export before switching. A new native start refuses to silently create an empty database alongside an unmigrated Docker database.

Do not run Docker and native app instances concurrently against shared application files. To return to Docker after native writes, use a deliberate logical database export/restore; restarting the original Docker database shows its original snapshot. Keep the original volumes and migration backups until you have verified your data. Direct `docker compose up` bypasses the Makefile migration preflight, so migrate first.

## Important files

| File | Purpose |
| --- | --- |
| `setup.sh` | First-time hardware detection, configuration, Ollama setup, and model downloads |
| `detect-hardware.sh` | Write detected hardware information to `data/hardware.json` |
| `startup.sh` | Create missing configuration, start Ollama/host runtime, and ensure the sandbox image exists |
| `profile-helper.py` | Query and validate the root model/module YAML files |
| `install-voice-models.py` | Delegate speech dependency/model installation to the backend installer |
| `host-runtime-server.py` | Native FastAPI runtime for ASR/TTS, custom voices, and sandbox operations |
| `sandbox_runtime.py` | Docker containers, persistent workspaces under `data/sandboxes/`, file operations, execution, and previews |
| `start-native.sh` / `start-native.ps1` | Native setup and foreground startup for normal/dev modes |
| `native/` | Native configuration, dependency preparation, service supervision and migration |
| `migrate-storage.py` | Retain and copy legacy Docker volumes; export the database for native restore |
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

The runtime has Docker-management capabilities. Keep it private and do not expose its port as a public API. Workspace deletion removes the corresponding persistent workspace.
