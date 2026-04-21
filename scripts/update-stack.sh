#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-$HOME/discord-bot}"

cd "$REPO_DIR"
git fetch origin main

LOCAL_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(git rev-parse origin/main)"

if [[ "$LOCAL_SHA" == "$REMOTE_SHA" ]]; then
  echo "discord-bot is already up to date at $LOCAL_SHA"
  exit 0
fi

git reset --hard "$REMOTE_SHA"
docker compose up -d --build
docker image prune -f
