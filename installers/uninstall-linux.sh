#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
docker compose --profile telegram --profile production down
