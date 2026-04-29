#!/bin/bash
set -euo pipefail

# =============================================================================
# Health check script — verifies all services are running
# =============================================================================

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

check_url() {
    local name=$1
    local url=$2
    local expected_status=${3:-200}
    
    local status
    status=$(curl -s -o /dev/null -w "%{http_code}" "$url" 2>/dev/null || echo "000")
    
    if [[ "$status" == "$expected_status" ]]; then
        echo -e "${GREEN}[OK]${NC}   $name ($url) — status $status"
        return 0
    else
        echo -e "${RED}[FAIL]${NC} $name ($url) — status $status (expected $expected_status)"
        return 1
    fi
}

echo "=== RealOpen-AI Health Check ==="
echo ""

FAILED=0

# Check backend
check_url "Backend API" "http://localhost:8000/api/health" || ((FAILED++))

# Check Ollama (native)
check_url "Ollama" "http://localhost:11434/api/version" || ((FAILED++))

# Check PostgreSQL (via backend health if we add DB check)
docker exec realopen-postgres pg_isready -U realopen &>/dev/null
if [[ $? -eq 0 ]]; then
    echo -e "${GREEN}[OK]${NC}   PostgreSQL — ready"
else
    echo -e "${RED}[FAIL]${NC} PostgreSQL — not ready"
    ((FAILED++))
fi

# Check Redis
docker exec realopen-redis redis-cli ping &>/dev/null
if [[ $? -eq 0 ]]; then
    echo -e "${GREEN}[OK]${NC}   Redis — ready"
else
    echo -e "${RED}[FAIL]${NC} Redis — not ready"
    ((FAILED++))
fi

# Check SearxNG
check_url "SearxNG" "http://localhost:8080/healthz" || ((FAILED++))

# Check Frontend (dev mode)
check_url "Frontend (Dev)" "http://localhost:5173" || ((FAILED++))

echo ""
if [[ $FAILED -eq 0 ]]; then
    echo -e "${GREEN}All services healthy!${NC}"
else
    echo -e "${RED}$FAILED service(s) failed health check${NC}"
    exit 1
fi