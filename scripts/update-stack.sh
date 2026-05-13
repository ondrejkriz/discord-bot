#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-$HOME/discord-bot}"
DEPLOYED_SHA_FILE="$REPO_DIR/.last_deployed_commit"

cd "$REPO_DIR"
git fetch origin main

LOCAL_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(git rev-parse origin/main)"
DEPLOYED_SHA=""

if [[ -f "$DEPLOYED_SHA_FILE" ]]; then
  DEPLOYED_SHA="$(<"$DEPLOYED_SHA_FILE")"
fi

if [[ "$LOCAL_SHA" == "$REMOTE_SHA" ]]; then
  echo "git checkout is already at $LOCAL_SHA"
else
  git reset --hard "$REMOTE_SHA"
  LOCAL_SHA="$REMOTE_SHA"
fi

if [[ "$DEPLOYED_SHA" == "$LOCAL_SHA" ]]; then
  echo "discord-bot is already deployed at $LOCAL_SHA"
  exit 0
fi

docker compose up -d --build
printf '%s\n' "$LOCAL_SHA" > "$DEPLOYED_SHA_FILE"
docker image prune -f
