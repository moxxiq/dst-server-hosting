#!/usr/bin/env bash
# Backup now: local zip + R2 upload. Usage: scripts/backup.sh [tag]   (tag default: manual)
set -Eeuo pipefail
TAG="${1:-manual}"
curl -fsS -X POST "http://127.0.0.1:8081/backup?tag=${TAG}" | python3 -m json.tool
