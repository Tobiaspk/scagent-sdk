#!/usr/bin/env bash

set -euo pipefail

# setup
PIXI_ENVS=(
    rapids
    cellbender
    diffxpy
)

# pass --update-lock-file to re-solve pixi.lock instead of requiring it up-to-date
LOCKED_ARGS=(--locked)
for arg in "$@"; do
    if [[ "$arg" == "--update-lock-file" ]]; then
        LOCKED_ARGS=()
    fi
done

# setup pixi paths (PIXI_MANIFEST, PIXI_EXEC, PIXI_SCAGENT) from .env
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.env"

mkdir -p $PIXI_SCAGENT/envs $PIXI_SCAGENT/cache

# setup pixi env
export PIXI_CACHE_NETFS_REDIRECT=never
export PIXI_CACHE_DIR=$PIXI_SCAGENT/cache
$PIXI_EXEC config set --local detached-environments $PIXI_SCAGENT/envs


# install & test
for PIXI_ENV in "${PIXI_ENVS[@]}"; do
    $PIXI_EXEC install \
        "${LOCKED_ARGS[@]}" \
        --environment "$PIXI_ENV" \
        --manifest-path "$PIXI_MANIFEST"

    $PIXI_EXEC run \
        "${LOCKED_ARGS[@]}" \
        --environment "$PIXI_ENV" \
        --manifest-path "$PIXI_MANIFEST" \
        check
done
