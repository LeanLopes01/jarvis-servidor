#!/usr/bin/env bash
# Publishes the JARVIS backend to the home server (run from Git Bash on the PC).
#
#   deploy/deploy.sh             lint, test, package, upload, install (asks for sudo)
#   deploy/deploy.sh --rollback  return the server to the previous release
#
# The package is an explicit file list: never the Flutter app, virtual
# environments, caches or secrets.
set -euo pipefail

SERVER=${JARVIS_SERVER:-user@your-server-ip}
cd "$(dirname "$0")/.."

if [ "${1:-}" = "--rollback" ]; then
    exec ssh -t "$SERVER" sudo jarvis-deploy --rollback
fi

uv run ruff check src tests deploy
uv run python -m pytest -q

files=(
    src/*.py src/jarvis_control/*.py src/jarvis_desktop/*.py
    tests/*.py
    deploy/jarvis-deploy deploy/jarvis-control deploy/smoke_session.py
    deploy/systemd/*.service deploy/polkit/*.rules
    pyproject.toml uv.lock scenarios.yaml README.md LICENSE .env.example
)
suffix=""
if [ -n "$(git status --porcelain -- "${files[@]}")" ]; then
    suffix="-wip"  # built from uncommitted changes
fi
version="$(date +%Y%m%dT%H%M%S)-$(git rev-parse --short HEAD)$suffix"
package="jarvis-backend-$version.tar.gz"
workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT

tar --force-local -czf "$workdir/$package" "${files[@]}"
checksum=$(sha256sum "$workdir/$package" | cut -d' ' -f1)
echo "package $package ($checksum)"

scp -q "$workdir/$package" "$SERVER:/tmp/$package"
ssh -t "$SERVER" "sudo jarvis-deploy /tmp/$package $checksum"
