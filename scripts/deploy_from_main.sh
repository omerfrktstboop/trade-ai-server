#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="/home/ubuntu/trade-ai-server"
VENV="$REPO_ROOT/.venv"
TARGET_SHA="${1:-origin/main}"
PORT=8080

log() {
  printf '[trade-ai-deploy] %s\n' "$*"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'missing required command: %s\n' "$1" >&2
    exit 1
  }
}

require_cmd git
require_cmd python3
require_cmd pip
require_cmd curl
require_cmd systemctl

if [ ! -d "$REPO_ROOT/.git" ]; then
  echo "repo not found at $REPO_ROOT" >&2
  exit 1
fi

cd "$REPO_ROOT"

log "fetching origin/main"
git fetch --prune origin main

log "checking out main"
git checkout main
log "resetting working tree to $TARGET_SHA"
git reset --hard "$TARGET_SHA"
log "cleaning untracked files"
git clean -fd

log "preparing virtualenv"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$REPO_ROOT/requirements.txt"

log "running syntax check"
"$VENV/bin/python" -m py_compile "$REPO_ROOT/app/main.py"

log "restarting trade-ai-server service"
sudo systemctl restart trade-ai-server

log "waiting for health check"
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS "http://127.0.0.1:$PORT/api/health" >/tmp/trade-ai-health.json 2>/dev/null; then
    cat /tmp/trade-ai-health.json
    rm -f /tmp/trade-ai-health.json
    log "deploy successful"
    exit 0
  fi
  sleep 3
done

echo "health check failed after restart" >&2
sudo systemctl status --no-pager trade-ai-server >&2 || true
exit 1
