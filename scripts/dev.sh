#!/usr/bin/env bash
# open-hospitality dev environment: start / stop / restart / status / logs
#
# Manages, in dependency order:
#   docker compose  -> postgres (127.0.0.1:5433) + keycloak (127.0.0.1:9080)
#   alembic         -> migrates the DB to head once postgres is ready
#   usali serve     -> API on 127.0.0.1:8100 (requires keycloak's realm)
#   mock providers  -> qbo-mock :9200, gusto-mock :9300, adp-mock :9301,
#                      delphi-mock :9400, tripleseat-mock :9401
#   vite            -> frontend dev server on 127.0.0.1:5173 (proxies /api)
#
# Background processes write pidfiles + logs under .dev/ (gitignored).
# `stop` TERMs each pid (KILL after a grace period) and stops the containers
# (data volumes are preserved; use `docker compose down -v` yourself if you
# really want to wipe the database).
#
# Usage: scripts/dev.sh start [--no-mocks] [--no-frontend]
#        scripts/dev.sh stop | restart | status | logs [name]

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV_DIR="$ROOT/.dev"
LOG_DIR="$DEV_DIR/logs"
mkdir -p "$LOG_DIR"

API_PORT=8100
VITE_PORT=5173
PG_PORT=5433
KC_REALM_URL="http://127.0.0.1:9080/realms/usali"

# name -> command (backgrounded members; docker compose is handled separately)
SERVICES=(api qbo-mock gusto-mock adp-mock delphi-mock tripleseat-mock frontend)

cmd_for() {
  case "$1" in
    api)        echo "uv run usali serve" ;;
    qbo-mock)   echo "uv run usali qbo-mock" ;;
    gusto-mock) echo "uv run usali gusto-mock" ;;
    adp-mock)   echo "uv run usali adp-mock" ;;
    delphi-mock)     echo "uv run usali delphi-mock" ;;
    tripleseat-mock) echo "uv run usali tripleseat-mock" ;;
    frontend)   echo "npm run dev" ;;
  esac
}

dir_for() {
  case "$1" in
    frontend) echo "$ROOT/frontend" ;;
    *)        echo "$ROOT" ;;
  esac
}

port_for() {
  case "$1" in
    api)        echo "$API_PORT" ;;
    qbo-mock)   echo 9200 ;;
    gusto-mock) echo 9300 ;;
    adp-mock)   echo 9301 ;;
    delphi-mock)     echo 9400 ;;
    tripleseat-mock) echo 9401 ;;
    frontend)   echo "$VITE_PORT" ;;
  esac
}

pidfile() { echo "$DEV_DIR/$1.pid"; }

alive() {
  local pf
  pf="$(pidfile "$1")"
  [[ -f "$pf" ]] && kill -0 "$(cat "$pf")" 2>/dev/null
}

port_listening() {
  # lsof is present on macOS and most Linux dev boxes.
  lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

wait_for() {
  # wait_for <description> <timeout-seconds> <command...>
  local what="$1" timeout="$2" waited=0
  shift 2
  until "$@" >/dev/null 2>&1; do
    if (( waited >= timeout )); then
      echo "ERROR: timed out after ${timeout}s waiting for $what" >&2
      return 1
    fi
    sleep 1
    (( waited++ )) || true
  done
  echo "  ready: $what"
}

start_bg() {
  local name="$1" port cmd dir
  port="$(port_for "$name")"
  cmd="$(cmd_for "$name")"
  dir="$(dir_for "$name")"
  if alive "$name"; then
    echo "  already running: $name (pid $(cat "$(pidfile "$name")"))"
    return 0
  fi
  if port_listening "$port"; then
    echo "ERROR: port $port is in use but $name has no pidfile — is it running outside this script?" >&2
    return 1
  fi
  ( cd "$dir" && exec $cmd ) >>"$LOG_DIR/$name.log" 2>&1 &
  echo $! >"$(pidfile "$name")"
  echo "  started: $name (pid $!, log .dev/logs/$name.log)"
}

stop_bg() {
  local name="$1" pf pid
  pf="$(pidfile "$name")"
  [[ -f "$pf" ]] || return 0
  pid="$(cat "$pf")"
  if kill -0 "$pid" 2>/dev/null; then
    # Kill the whole process group when possible (vite/uvicorn spawn children).
    kill -TERM -- -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 10); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL -- -"$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi
    echo "  stopped: $name"
  fi
  rm -f "$pf"
}

do_start() {
  local with_mocks=1 with_frontend=1
  for arg in "$@"; do
    case "$arg" in
      --no-mocks) with_mocks=0 ;;
      --no-frontend) with_frontend=0 ;;
      *) echo "unknown flag: $arg" >&2; exit 2 ;;
    esac
  done

  echo "[1/4] containers (postgres + keycloak)"
  (cd "$ROOT" && docker compose up -d)
  wait_for "postgres :$PG_PORT" 60 pg_isready -h 127.0.0.1 -p "$PG_PORT" ||
    wait_for "postgres :$PG_PORT (via port)" 60 port_listening "$PG_PORT"
  wait_for "keycloak realm" 120 curl -fsS "$KC_REALM_URL"

  echo "[2/4] database migration"
  # L2: the app role first — initdb scripts run only on a FRESH volume,
  # and the l2a0rlswall migration refuses without the role, so re-apply
  # the (idempotent) init to converge existing volumes.
  (cd "$ROOT" && docker compose exec -T postgres \
    psql -q -U usali -d usali \
    -f /docker-entrypoint-initdb.d/10-usali-app-role.sql)
  (cd "$ROOT" && uv run alembic upgrade head)

  echo "[3/4] backend + mocks"
  start_bg api
  wait_for "api :$API_PORT" 60 port_listening "$API_PORT"
  if (( with_mocks )); then
    start_bg qbo-mock
    start_bg gusto-mock
    start_bg adp-mock
    start_bg delphi-mock
    start_bg tripleseat-mock
  fi

  echo "[4/4] frontend"
  if (( with_frontend )); then
    start_bg frontend
    wait_for "vite :$VITE_PORT" 60 port_listening "$VITE_PORT"
    echo
    # vite binds [::1], so localhost (not 127.0.0.1) is the working URL
    echo "Dev environment up: http://localhost:$VITE_PORT"
  else
    echo
    echo "Dev environment up (API only): http://127.0.0.1:$API_PORT"
  fi
}

do_stop() {
  for name in frontend tripleseat-mock delphi-mock adp-mock gusto-mock qbo-mock api; do
    stop_bg "$name"
  done
  echo "containers:"
  (cd "$ROOT" && docker compose stop)
  echo "stopped (DB volume preserved; 'docker compose down -v' wipes it)"
}

do_status() {
  printf "%-12s %-9s %s\n" "SERVICE" "STATE" "DETAIL"
  for name in "${SERVICES[@]}"; do
    local_state="stopped"
    detail="-"
    if alive "$name"; then
      local_state="running"
      detail="pid $(cat "$(pidfile "$name")"), port $(port_for "$name")"
    elif port_listening "$(port_for "$name")"; then
      local_state="foreign"
      detail="port $(port_for "$name") in use, not ours"
    fi
    printf "%-12s %-9s %s\n" "$name" "$local_state" "$detail"
  done
  echo "containers:"
  (cd "$ROOT" && docker compose ps --format '  {{.Service}}: {{.State}}' 2>/dev/null) || true
}

do_logs() {
  local name="${1:-api}"
  exec tail -F "$LOG_DIR/$name.log"
}

case "${1:-}" in
  start)   shift; do_start "$@" ;;
  stop)    do_stop ;;
  restart) do_stop; shift; do_start "$@" ;;
  status)  do_status ;;
  logs)    shift || true; do_logs "$@" ;;
  *)
    echo "usage: scripts/dev.sh start [--no-mocks] [--no-frontend] | stop | restart | status | logs [service]" >&2
    echo "services: ${SERVICES[*]}" >&2
    exit 2
    ;;
esac
