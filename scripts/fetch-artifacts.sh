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
    && node make-pgdata.mjs "$WORK/pgdata")
if [ -e "$WORK/pgdata/postmaster.pid" ]; then
    echo "error: postmaster.pid left behind: PGDATA was not shut down cleanly" >&2
    exit 1
fi
tar -C "$WORK/pgdata" --owner=0 --group=0 --numeric-owner -czf "$OUT/pgdata.tar.gz" .
# provenance of the artifacts
cat > "$OUT/BUILD_INFO" <<END
pglite_commit=$(git -C "$PGLITE_REPO" rev-parse HEAD)
postgres_pglite_commit=$(git -C "$PGLITE_REPO/postgres-pglite" rev-parse HEAD)
pgdata_npm_version=@electric-sql/pglite@$VERSION
END
ls -la "$OUT"
