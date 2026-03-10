#!/usr/bin/env bash

ICARUS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if grep -q "icarus()" ~/.bashrc 2>/dev/null; then
    echo "icarus() is already defined in ~/.bashrc. Skipping."
    exit 0
fi

cat >> ~/.bashrc << EOF

icarus() {
    local cmd="\${1:-help}"
    case "\$cmd" in
        wake)
            "$ICARUS_DIR/start.sh"
            ;;
        logs)
            docker compose -f "$ICARUS_DIR/docker-compose.yml" logs -f icarus-api
            ;;
        stop)
            docker compose -f "$ICARUS_DIR/docker-compose.yml" down
            ;;
        *)
            echo "Usage: icarus <command>"
            echo "  wake   Start Docker services + Councilor"
            echo "  stop   Stop Docker services"
            echo "  logs   Tail icarus-api logs"
            ;;
    esac
}
EOF

echo "Done. Run: source ~/.bashrc"
