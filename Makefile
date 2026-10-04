COMPOSE			= docker compose
COMPOSE_DEV		= $(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml
COMPOSE_MON		= $(COMPOSE) -f docker-compose.yml -f docker-compose.monitoring.yml

# Default: auto-setup if needed, then dev environment with hot-reloading
.PHONY: all
all: dev

# ── First-time setup: detect hardware, install Ollama, pull model ──
# Auto-detects whether setup is needed by checking data/.setup-complete
.PHONY: setup
setup:
	@bash scripts/setup.sh

# ── Run hardware detection only (writes data/hardware.json) ──
.PHONY: detect-hardware
detect-hardware:
	@bash scripts/detect-hardware.sh

# ── Startup script (runs on container start) ──
.PHONY: startup
startup:
	@bash scripts/startup.sh

.PHONY: voice-install
voice-install:
	@cd backend && poetry run python ../scripts/install-voice-models.py

.PHONY: sandbox-install
sandbox-install:
	@cd backend && poetry install

.PHONY: sandbox-build
sandbox-build:
	@docker build -t realopenai-sandbox:latest sandbox

# ── Development mode (hot reload, debug ports exposed) ──
.PHONY: dev
dev: startup detect-hardware
	@echo "Starting RealOpen-AI in development mode..."
	@$(COMPOSE_DEV) up --build

# ── Development mode (detached) ──
.PHONY: dev-d
dev-d: startup detect-hardware
	@echo "Starting RealOpen-AI in development mode (detached)..."
	@$(COMPOSE_DEV) up --build -d
	@echo ""
	@echo "  Frontend:	http://localhost:5173"
	@echo "  Backend:	http://localhost:8000"
	@echo "  API Docs:	http://localhost:8000/docs"
	@echo ""

# ── Production mode ──
.PHONY: up
up: startup detect-hardware
	@echo "Starting RealOpen-AI in production mode..."
	@$(COMPOSE) up --build -d
	@echo ""
	@echo "  App:		http://localhost"
	@echo "  API Docs:	http://localhost/api/docs (proxied)"
	@echo ""

# ── Stop all services ──
.PHONY: down
down:
	@$(COMPOSE_DEV) down 2>/dev/null || $(COMPOSE) down

# ── Stop monitoring stack only ──
.PHONY: down-mon
down-mon:
	@$(COMPOSE_MON) down 2>/dev/null || true

# ── Restart all services ──
.PHONY: re
re: down all

# ── Stop and remove volumes (full reset) ──
.PHONY: clean
clean:
	@echo "Removing all containers, volumes, and images..."
	@$(COMPOSE) down -v --rmi local 2>/dev/null || true
	@$(COMPOSE_DEV) down -v --rmi local 2>/dev/null || true

# ── Monitoring stack ──
.PHONY: monitor
monitor: startup detect-hardware
	@echo "Starting monitoring stack..."
	@$(COMPOSE_MON) up -d
	@echo ""
	@echo "  App:			http://localhost"
	@echo "  API Docs:		http://localhost/api/docs (proxied)"
	@echo "  Grafana:		http://localhost:3000"
	@echo "  Victoria Metrics:	http://localhost:8428"
	@echo ""

# ── Stop monitoring ──
.PHONY: monitor-down
monitor-down:
	@$(COMPOSE_MON) down

# ── Ngrok tunnel ──
.PHONY: tunnel
tunnel:
	@$(COMPOSE) --profile tunnel up -d ngrok
	@echo "Check tunnel URL: docker logs realopen-ngrok 2>&1 | grep https"

# ── View logs ──
.PHONY: logs
logs:
	@$(COMPOSE) logs -f

# ── View backend logs ──
.PHONY: logs-backend
logs-backend:
	@$(COMPOSE) logs -f backend

# ── Shell into backend container ──
.PHONY: shell-backend
shell-backend:
	@docker exec -it realopen-backend bash

# ── Shell into PostgreSQL ──
.PHONY: shell-db
shell-db:
	@docker exec -it realopen-postgres psql -U realopen -d realopen

# ── Run Alembic migration ──
.PHONY: migrate
migrate:
	@docker exec realopen-backend alembic upgrade head

# ── Create new Alembic migration ──
.PHONY: migration
migration:
	@read -p "Migration message: " msg; \
	docker exec realopen-backend alembic revision --autogenerate -m "$$msg"

# ── Health check ──
.PHONY: health
health:
	@bash scripts/health-check.sh

# ── Install frontend dependencies ──
.PHONY: npm-install
npm-install:
	@cd frontend && npm install && npm audit fix

# ── Pull ALL models for the current hardware profile ──
.PHONY: pull-models
pull-models:
	@echo "Pulling all models for current profile..."
	@bash -c 'PROFILE=$$(grep "^HARDWARE_PROFILE=" .env 2>/dev/null | cut -d= -f2 || echo cpu_small); \
		python3 scripts/profile-helper.py $$PROFILE all-models | while read model; do \
			echo "Pulling $$model..."; ollama pull $$model; \
		done'

# ── Validate profiles.yml ──
.PHONY: validate-profiles
validate-profiles:
	@python3 scripts/profile-helper.py cpu_small validate

# ── Validate modules.yml ──
.PHONY: validate-modules
validate-modules:
	@python3 scripts/profile-helper.py modules cpu_small validate-modules

# ── List available modules ──
.PHONY: list-modules
list-modules:
	@python3 scripts/profile-helper.py modules cpu_small list

# ── Pull models for optional modules ──
.PHONY: pull-module-models
pull-module-models:
	@echo "Pulling optional module models for current profile..."
	@bash -c 'PROFILE=$$(grep "^HARDWARE_PROFILE=" .env 2>/dev/null | cut -d= -f2 || echo cpu_small); \
		python3 scripts/profile-helper.py modules $$PROFILE all-module-models | while read model; do \
			echo "Pulling $$model..."; ollama pull $$model; \
		done'

# ── Reset setup (delete marker to re-run setup wizard on next start) ──
.PHONY: reset-setup
reset-setup:
	@rm -f data/.setup-complete
	@echo "Setup marker removed. Run 'make setup' or restart the app to go through setup again."

# ── Tests (local dependencies required; no running Ollama needed) ──
# Install with: cd backend && poetry install --with dev
#               cd frontend && npm ci
# Optional filters: make test-backend PYTEST_ARGS='tests/test_core_metrics.py'
PYTEST_ARGS ?=
COVERAGE_MIN ?= 80

.PHONY: test-backend test-frontend test coverage
test-backend:
	cd backend && poetry run pytest -q $(PYTEST_ARGS)

test-frontend:
	cd frontend && npm test

test: test-backend test-frontend

# Backend coverage gate, matching CI; frontend currently has no coverage gate.
coverage:
	cd backend && poetry run pytest -q --cov=app --cov-report=term-missing --cov-fail-under=$(COVERAGE_MIN) $(PYTEST_ARGS)
