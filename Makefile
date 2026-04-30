COMPOSE			= docker compose
COMPOSE_DEV		= $(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml
COMPOSE_MON		= $(COMPOSE) -f docker-compose.yml -f docker-compose.monitoring.yml

# Default: dev environment with hot-reloading
.PHONY: all
all: setup dev

# ── First-time setup: detect hardware, install Ollama, pull model ──
.PHONY: setup
setup:
	@bash scripts/setup.sh

# ── Development mode (hot reload, debug ports exposed) ──
.PHONY: dev
dev:
	@echo "Starting RealOpen-AI in development mode..."
	@$(COMPOSE_DEV) up --build

# ── Development mode (detached) ──
.PHONY: dev-d
dev-d:
	@echo "Starting RealOpen-AI in development mode (detached)..."
	@$(COMPOSE_DEV) up --build -d
	@echo ""
	@echo "  Frontend:	http://localhost:5173"
	@echo "  Backend:	http://localhost:8000"
	@echo "  API Docs:	http://localhost:8000/docs"
	@echo ""

# ── Production mode ──
.PHONY: up
up:
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

# ── Stop and remove volumes (full reset) ──
.PHONY: clean
clean:
	@echo "Removing all containers, volumes, and images..."
	@$(COMPOSE) down -v --rmi local 2>/dev/null || true
	@$(COMPOSE_DEV) down -v --rmi local 2>/dev/null || true

# ── Monitoring stack ──
.PHONY: monitor
monitor:
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
	@cd frontend && npm install

# ── Pull ALL models for the current hardware profile ──
.PHONY: pull-models
pull-models:
	@echo "Pulling all models for current profile..."
	@bash -c 'PROFILE=$$(grep "^HARDWARE_PROFILE=" .env 2>/dev/null | cut -d= -f2 || echo 8gb); \
		python3 scripts/profile-helper.py $$PROFILE all-models | while read model; do \
			echo "Pulling $$model..."; ollama pull $$model; \
		done'

# ── Validate profiles.yml ──
.PHONY: validate-profiles
validate-profiles:
	@python3 scripts/profile-helper.py 8gb validate
