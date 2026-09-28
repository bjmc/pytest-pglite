#!/bin/bash
# Collect the standalone PGlite build artifacts into src/pglite_wasm/_artifacts:
#   - pglite-standalone.wasm, pglite-standalone-fs.tar.gz: from a pglite checkout,
#     built with `pnpm wasm:build:standalone`
#   - pgdata.tar.gz: an initialized PGDATA, created with the JS build of PGlite
#     (the npm release matching the pglite checkout)
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
PGLITE_REPO=${PGLITE_REPO:-"$HERE/../pglite"}
OUT="$HERE/src/pglite_wasm/_artifacts"
RELEASE="$PGLITE_REPO/packages/pglite-standalone/release"

mkdir -p "$OUT"
cp "$RELEASE/pglite-standalone.wasm" "$RELEASE/pglite-standalone-fs.tar.gz" "$OUT/"

VERSION=$(node -p "require('$PGLITE_REPO/packages/pglite/package.json').version")
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cp "$HERE/scripts/make-pgdata.mjs" "$WORK/"
(cd "$WORK" && npm init -y >/dev/null && npm install --silent "@electric-sql/pglite@$VERSION" \
    && node make-pgdata.mjs "$OUT/pgdata.tar.gz")
ls -la "$OUT"
