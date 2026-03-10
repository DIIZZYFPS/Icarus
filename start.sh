#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cleanup() {
    echo ""
    echo "[Icarus] Shutting down Docker services..."
    docker compose -f "$SCRIPT_DIR/docker-compose.yml" down
    exit 0
}

trap cleanup INT TERM

echo "[Icarus] Starting Docker services..."
docker compose -f "$SCRIPT_DIR/docker-compose.yml" up -d

echo "[Icarus] Docker services running. Starting Councilor..."
echo ""

python "$SCRIPT_DIR/councilor.py"
