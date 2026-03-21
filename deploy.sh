#!/bin/bash
set -e

COMMAND=$1
PROJECT_NAME="combflow"
COMPOSE="docker-compose"
APP_CONTAINER="combflow_combflow-app_1"

usage() {
    echo "Usage: $0 [command]"
    echo
    echo "Commands:"
    echo "  up       Build and start all services."
    echo "  down     Stop and remove containers."
    echo "  restart  Stop then start all services."
    echo "  logs     Follow logs from all services."
    echo "  status   Show service health and stats."
    echo "  backup   Dump the database to a timestamped SQL file."
    echo "  reset    Stop, wipe DB volume, rebuild and start fresh."
    echo "  clean    Remove containers, volumes, and images (nuclear option)."
}

check_dependencies() {
    if ! command -v docker &> /dev/null; then
        echo "Error: Docker is not installed."
        exit 1
    fi
    # Prefer 'docker compose' (v2 plugin) over 'docker-compose' (v1 standalone).
    if docker compose version &> /dev/null 2>&1; then
        COMPOSE="docker compose"
    elif ! command -v docker-compose &> /dev/null; then
        echo "Error: Docker Compose is not installed."
        exit 1
    fi
}

up() {
    echo "Starting ${PROJECT_NAME}..."
    # Remove stale containers to avoid ContainerConfig KeyError.
    docker rm -f ${APP_CONTAINER} 2>/dev/null || true
    $COMPOSE up -d --build
    echo
    SITE="${CADDY_UI:-honeycomb.lvh.me:80}"
    DOMAIN="${SITE%%:*}"
    echo "${PROJECT_NAME} started."
    echo "  UI:     http://${DOMAIN}/"
    echo "  API:    http://${DOMAIN}/docs"
    echo "  Health: http://${DOMAIN}/health"
}

down() {
    echo "Stopping ${PROJECT_NAME}..."
    $COMPOSE down
    echo "${PROJECT_NAME} stopped."
}

restart() {
    down
    up
}

logs() {
    $COMPOSE logs -f "$@"
}

status() {
    echo "=== Service status ==="
    $COMPOSE ps
    echo
    echo "=== Health check ==="
    SITE="${CADDY_UI:-honeycomb.lvh.me:80}"
    DOMAIN="${SITE%%:*}"
    curl -sf "http://${DOMAIN}/health" 2>/dev/null && echo || echo "App not reachable"
    echo
    echo "=== Stats ==="
    curl -sf "http://${DOMAIN}/api/stats" 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "Could not fetch stats"
}

backup() {
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    BACKUP_FILE="backup_${TIMESTAMP}.sql"
    echo "Dumping database to ${BACKUP_FILE}..."
    $COMPOSE exec -T db pg_dump -U "${POSTGRES_USER:-combflow}" "${POSTGRES_DB:-combflow}" > "${BACKUP_FILE}"
    echo "Backup saved: ${BACKUP_FILE}"
}

reset() {
    echo "This will stop services, wipe the database, and rebuild."
    read -r -p "Are you sure? [y/N] " confirm
    if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
        echo "Aborted."
        exit 0
    fi
    $COMPOSE down -v
    up
}

clean() {
    echo "This will DELETE all containers, volumes, and images for ${PROJECT_NAME}."
    read -r -p "Are you sure? [y/N] " confirm
    if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
        echo "Aborted."
        exit 0
    fi
    $COMPOSE down --volumes --rmi all
    echo "${PROJECT_NAME} cleaned."
}

check_dependencies

case "$COMMAND" in
    up)      up      ;;
    down)    down    ;;
    restart) restart ;;
    logs)    shift; logs "$@" ;;
    status)  status  ;;
    backup)  backup  ;;
    reset)   reset   ;;
    clean)   clean   ;;
    *)       usage; exit 1 ;;
esac
