#!/usr/bin/env bash
# Reproducible host bootstrap for n64to3ds.
#
# This script prepares the Python dependencies and the pinned Daedalus tree.
# devkitPro itself is deliberately not downloaded from an unpinned source:
# install it with the official devkitPro pacman repository, or use
# --from-image to extract the exact pinned Daedalus build image (Docker
# required).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DAEDALUS_DIR="$ROOT/third_party/daedalus"
DAEDALUS_COMMIT="580c0c409358db332149fc2903c610449434f8fc"
DAEDALUS_URL="https://github.com/DaedalusX64/daedalus.git"
IMAGE="wally4000/daedalusx64@sha256:e80de13da91fdb69417f2a05b27f25245dc238d1488f5b15e07990143dce503c"
PATCH="$ROOT/patches/daedalus-n64to3ds.patch"

usage() {
    cat <<'EOF'
usage: scripts/bootstrap.sh [--from-image]

  --from-image  extract devkitPro and makerom from the pinned Daedalus Docker
                image into third_party/devkitpro-root (requires docker or podman)
EOF
}

from_image=0
for arg in "$@"; do
    case "$arg" in
        --from-image) from_image=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

echo "== Python dependencies =="
python3 -m pip install --user -r "$ROOT/requirements.txt"

echo "== Pinned Daedalus tree =="
if [ ! -d "$DAEDALUS_DIR/.git" ]; then
    mkdir -p "$ROOT/third_party"
    git clone "$DAEDALUS_URL" "$DAEDALUS_DIR"
fi
git -C "$DAEDALUS_DIR" fetch --tags origin
git -C "$DAEDALUS_DIR" checkout --detach "$DAEDALUS_COMMIT"

if git -C "$DAEDALUS_DIR" apply --reverse --check "$PATCH" >/dev/null 2>&1; then
    echo "patch already applied"
elif git -C "$DAEDALUS_DIR" apply --check "$PATCH"; then
    git -C "$DAEDALUS_DIR" apply "$PATCH"
    echo "applied $PATCH"
else
    echo "error: $PATCH does not apply to the pinned tree." >&2
    echo "The checkout must be exactly $DAEDALUS_COMMIT." >&2
    exit 1
fi

if [ "$from_image" -eq 1 ]; then
    echo "== Extracting pinned devkitPro image =="
    if command -v docker >/dev/null 2>&1; then
        container_engine=docker
    elif command -v podman >/dev/null 2>&1; then
        container_engine=podman
    else
        echo "error: --from-image requires docker or podman." >&2
        exit 1
    fi
    mkdir -p "$ROOT/third_party/devkitpro-root/opt" "$ROOT/third_party/devkitpro-root/usr/local/bin"
    container="$("$container_engine" create "$IMAGE")"
    trap '"$container_engine" rm -f "$container" >/dev/null 2>&1 || true' EXIT
    "$container_engine" cp "$container:/opt/devkitpro" "$ROOT/third_party/devkitpro-root/opt/"
    "$container_engine" cp "$container:/usr/local/bin/makerom" "$ROOT/third_party/devkitpro-root/usr/local/bin/makerom"
    ln -sfn devkitpro-root/opt/devkitpro "$ROOT/third_party/devkitpro"
    echo "extracted devkitPro into third_party/devkitpro"
else
    echo
    echo "devkitPro check:"
    if [ -n "${DEVKITPRO:-}" ] && [ -d "${DEVKITPRO}/devkitARM" ]; then
        echo "  using DEVKITPRO=$DEVKITPRO"
    elif [ -d "$ROOT/third_party/devkitpro/devkitARM" ]; then
        echo "  using $ROOT/third_party/devkitpro"
    else
        cat >&2 <<'EOF'
  devkitPro was not found.  Install it from the official devkitPro pacman
  repository (see README.md), set DEVKITPRO, or re-run with --from-image.
EOF
    fi
fi

echo
echo "Bootstrap complete.  Build with:"
echo "  scripts/build.sh build \"game.z64\" --target old3ds --output \"game.cia\""
