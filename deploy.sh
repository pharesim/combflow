#!/bin/bash
set -e

COMMAND=$1
PROJECT_NAME="combflow"
COMPOSE="docker-compose"

usage() {
    echo "Usage: $0 [command]"
    echo
    echo "Commands:"
    echo "  up                  Build and start all services (cold start)."
    echo "  deploy [--pull]     Rebuild + zero-downtime rolling update."
    echo "                      --pull also runs git pull first."
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
    # Remove stale app container to avoid ContainerConfig KeyError (Compose v1+v2 compat)
    local cid
    cid=$($COMPOSE ps -q combflow-app 2>/dev/null || echo "")
    if [ -n "$cid" ]; then
        docker rm -f "$cid" 2>/dev/null || true
    fi
    $COMPOSE up -d --build
    echo
    SITE="${CADDY_UI:-honeycomb.lvh.me:80}"
    DOMAIN="${SITE%%:*}"
    echo "${PROJECT_NAME} started."
    echo "  UI:     http://${DOMAIN}/"
    echo "  API:    http://${DOMAIN}/docs"
    echo "  Health: http://${DOMAIN}/health"
}

rolling_swap_app() {
    # Zero-downtime rolling deploy of combflow-app:
    #   1. Start a new container alongside the old (scale to N+1)
    #   2. Wait for /health to pass on the new container
    #   3. Stop & remove the old container(s)
    # Caddy uses dynamic DNS (5s refresh) + lb_try_duration to pick up the
    # new IP and retry across the swap.
    local old_ids
    old_ids=$($COMPOSE ps -q combflow-app 2>/dev/null || echo "")
    if [ -z "$old_ids" ]; then
        echo "  no running combflow-app — starting normally"
        $COMPOSE up -d --no-deps combflow-app
        return
    fi
    local old_count new_count
    old_count=$(echo "$old_ids" | wc -l)
    new_count=$((old_count + 1))

    echo "  starting new container alongside (${old_count} -> ${new_count})..."
    $COMPOSE up -d --no-deps --no-recreate --scale combflow-app=$new_count combflow-app

    local all_ids new_id
    all_ids=$($COMPOSE ps -q combflow-app)
    new_id=""
    for id in $all_ids; do
        if ! echo "$old_ids" | grep -q "^${id}"; then
            new_id="$id"
            break
        fi
    done
    if [ -z "$new_id" ]; then
        echo "  ERROR: could not identify the new container."
        exit 1
    fi
    echo "  new container: ${new_id}"

    echo "  waiting for /health on the new container (up to 90s)..."
    local timeout=90
    while [ $timeout -gt 0 ]; do
        if docker exec "$new_id" curl -sf http://localhost:8000/health 2>/dev/null \
              | grep -q '"status":"ok"'; then
            echo "  healthy after $((90 - timeout))s."
            break
        fi
        sleep 1
        ((timeout--))
    done
    if [ $timeout -le 0 ]; then
        echo
        echo "ERROR: new container did not become healthy in 90s."
        echo "Old containers still serving traffic. Inspect:  docker logs ${new_id}"
        echo "Roll back:"
        echo "  docker stop ${new_id} && docker rm ${new_id}"
        echo "  $COMPOSE up -d --no-deps --scale combflow-app=${old_count} combflow-app"
        exit 1
    fi

    # One DNS refresh interval so Caddy picks up the new IP before old goes away.
    sleep 6

    echo "  stopping old container(s)..."
    for id in $old_ids; do
        docker stop "$id" >/dev/null
        docker rm "$id" >/dev/null
        echo "    stopped ${id}"
    done
}

deploy() {
    # Full deploy — everything, in dependency order, with rolling swaps where
    # downtime would be user-visible. Designed to be the single command for
    # all updates (code, third-party images, Caddyfile, infra).
    #
    # Order:
    #   1. (optional --pull) git pull
    #   2. Pull third-party images (caddy, goaccess) — no-op if up to date
    #   3. Build local images (combflow-app, hive_worker, prerender)
    #   4. Recreate caddy/goaccess if the image changed (compose up is a no-op
    #      if the image hash matches), then hot-reload Caddyfile (no downtime)
    #   5. Recreate hive_worker (brief gap, no user impact)
    #   6. Recreate prerender (brief gap for crawler requests — Caddy retries
    #      via lb_try_duration so most are absorbed; the cache survives the
    #      restart since it's on a docker volume)
    #   7. Rolling swap of combflow-app — zero user-facing downtime

    if [ "$1" = "--pull" ] || [ "$1" = "-p" ]; then
        echo "Pulling latest code..."
        git pull
        echo
    fi

    echo "Pulling third-party images..."
    $COMPOSE pull caddy goaccess
    echo

    echo "Building local images..."
    $COMPOSE build combflow-app hive_worker prerender
    echo

    echo "Updating caddy + goaccess (no-op if image unchanged)..."
    $COMPOSE up -d --no-deps caddy goaccess
    # Hot-reload Caddyfile so config-only changes don't need a container restart.
    if [ -n "$($COMPOSE ps -q caddy)" ]; then
        $COMPOSE exec -T caddy caddy reload \
            --config /etc/caddy/Caddyfile --adapter caddyfile 2>/dev/null || true
    fi
    echo

    echo "Recreating hive_worker (brief gap, no user impact)..."
    $COMPOSE up -d --no-deps --force-recreate hive_worker
    echo

    echo "Recreating prerender (brief gap, Googlebot only — Caddy retries)..."
    $COMPOSE up -d --no-deps --force-recreate prerender
    echo

    echo "Rolling swap of combflow-app (zero downtime)..."
    rolling_swap_app
    echo

    echo "${PROJECT_NAME} deployed."
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
    deploy)  shift; deploy "$@" ;;
    down)    down    ;;
    restart) restart ;;
    logs)    shift; logs "$@" ;;
    status)  status  ;;
    backup)  backup  ;;
    reset)   reset   ;;
    clean)   clean   ;;
    *)       usage; exit 1 ;;
esac
