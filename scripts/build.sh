#!/usr/bin/env bash
# Convenience wrapper that points devkitPro tools at the pinned tree.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -z "${DEVKITPRO:-}" ]; then
    if [ -d "$ROOT/third_party/devkitpro/devkitARM" ]; then
        DEVKITPRO="$ROOT/third_party/devkitpro"
    elif [ -d /opt/devkitpro/devkitARM ]; then
        DEVKITPRO=/opt/devkitpro
    else
        echo "error: set DEVKITPRO or run scripts/bootstrap.sh --from-image" >&2
        exit 1
    fi
fi
export DEVKITPRO
export DEVKITARM="${DEVKITARM:-$DEVKITPRO/devkitARM}"
export PATH="$ROOT/third_party/devkitpro-root/usr/local/bin:$DEVKITARM/bin:$DEVKITPRO/tools/bin:$PATH"

exec "$ROOT/n64to3ds" "$@"
