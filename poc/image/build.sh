#!/usr/bin/env bash
# Build the sandbox image with a checksum-pinned static pandoc.
# Picks the pandoc build that matches the Docker daemon's CPU architecture
# (amd64 on Intel hosts, arm64 on Apple silicon).
set -euo pipefail
cd "$(dirname "$0")"

IMAGE="${SANDBOX_IMAGE:-agent-sandbox:poc}"
VERSION="3.1.12.3"
BASE="https://github.com/jgm/pandoc/releases/download/${VERSION}"

case "$(docker info --format '{{.Architecture}}')" in
  x86_64)        ARCH=amd64; SHA=f80df137a070e0c05b7f6a3f8f1799124bacf15b57e05890488a7f49f614da09 ;;
  aarch64|arm64) ARCH=arm64; SHA=f80a4fca157f24ae6912cf8bed4b92fa2e5e5c5b98d0a7b675bc8536deb65602 ;;
  *) echo "unsupported docker architecture" >&2; exit 1 ;;
esac

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi; }

# The download is cached per version and architecture, and re-verified on every build, so a stale or
# swapped binary (e.g. after switching Docker context from amd64 to arm64) cannot reach the image.
mkdir -p build
TGZ="build/pandoc-${VERSION}-${ARCH}.tgz"
[ -f "$TGZ" ] || curl -fsSL -o "$TGZ" "${BASE}/pandoc-${VERSION}-linux-${ARCH}.tar.gz"
if [ "$(sha256 "$TGZ" | cut -d' ' -f1)" != "$SHA" ]; then
  echo "checksum mismatch for $TGZ (expected $SHA)" >&2; rm -f "$TGZ"; exit 1
fi
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
tar -xzf "$TGZ" -C "$tmp"
install -m 0755 "$tmp/pandoc-${VERSION}/bin/pandoc" build/pandoc
BASE_ARG="--build-arg BASE=${SANDBOX_BASE:-mirror.gcr.io/library/python:3.12-slim-bookworm}"
docker build $BASE_ARG --target full -t "$IMAGE" .
docker build $BASE_ARG --target base -t "${IMAGE}-nopandoc" .
