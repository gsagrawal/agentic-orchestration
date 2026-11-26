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

mkdir -p build
if [ ! -x build/pandoc ]; then
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  curl -fsSL -o "$tmp/pandoc.tgz" "${BASE}/pandoc-${VERSION}-linux-${ARCH}.tar.gz"
  echo "${SHA}  $tmp/pandoc.tgz" | shasum -a 256 -c -
  tar -xzf "$tmp/pandoc.tgz" -C "$tmp"
  install -m 0755 "$tmp/pandoc-${VERSION}/bin/pandoc" build/pandoc
fi
BASE_ARG="--build-arg BASE=${SANDBOX_BASE:-mirror.gcr.io/library/python:3.12-slim-bookworm}"
docker build $BASE_ARG --target full -t "$IMAGE" .
docker build $BASE_ARG --target base -t "${IMAGE}-nopandoc" .
