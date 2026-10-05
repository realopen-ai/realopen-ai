# Backend

FastAPI application for chat, agent orchestration, memory, retrieval, generated documents, skills, voice sessions, and coding workspaces.

The backend runs in Docker by default. Ollama and the native host runtime are separate host processes; the host runtime handles speech acceleration and Docker sandbox operations. See the [architecture overview](../README.md#architecture).

## Layout

| Directory | Responsibility |
| --- | --- |
| `app/api/` | HTTP and WebSocket endpoints |
| `app/agent/` | General agent loop, dedicated `coder/` loop, and tool adapters |
| `app/prompts/` | Shared and specialized Markdown prompts |
| `app/services/` | Providers, retrieval, memory, streams, skills, document generation, and persistence |
| `app/voice/` | ASR/TTS adapters, audio, VAD, echo cancellation, and voice session state |
| `app/db/` | SQLAlchemy models and async sessions |
| `app/core/` | Logging, middleware, metrics, and other shared infrastructure |
| `alembic/` | Database migrations |
| `tests/` | Unit and integration tests with mocked provider interactions |

`app/main.py` owns application setup and lifespan; `app/config.py` resolves settings and model profiles.

## Run

From the repository root:

```sh
make              # Docker development stack
make up           # Production stack
make logs-backend
```

Development exposes **http://localhost:8000**, including **/docs** and **/api/health**. Production reaches the backend through Nginx. Container startup applies Alembic migrations before starting Uvicorn.

Local dependency installation:

```sh
cd backend
poetry install --no-root --with dev
```

Avoid activating an unrelated virtual environment before using Poetry. Running the API outside Docker requires reachable PostgreSQL, cache/search endpoints, Ollama, and host-runtime settings; Compose service names are not host DNS names. Configuration is documented in [.env.example](../.env.example), [profiles.yml](../profiles.yml), and [modules.yml](../modules.yml).

## Streaming and persistence

Text uses SSE. Active generation is owned by the backend rather than the browser connection, with incremental message persistence and reconnectable events. Browser reload recovery requires the backend process to remain alive; active streams are not durable jobs across backend restarts.

Voice and terminals use WebSockets. Workspace command history is stored in PostgreSQL, while workspace files live in Docker-managed volumes. Imported local skills and host runtime resources are stored under the shared `data/` directory.

## Tests and checks

From the repository root:

```sh
make test-backend
make coverage
make test-backend PYTEST_ARGS='tests/test_core_metrics.py'
```

From this directory:

```sh
poetry run ruff check app tests
poetry run alembic heads
```

Coverage uses the same **80%** threshold as CI. Tests do not require actual Ollama inference. PDF rendering requires native WeasyPrint libraries; dependent tests skip if those libraries are unavailable locally. Docker/CI install the Linux rendering libraries.

Use `make migrate` to apply migrations and `make migration` to create one in the running backend container. Preserve a single Alembic head and include migrations for schema changes.
