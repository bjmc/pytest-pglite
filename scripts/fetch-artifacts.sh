#!/bin/bash
# Collect the standalone PGlite build artifacts into src/pytest_pglite/_artifacts:
#   - pglite-standalone.wasm, pglite-standalone-fs.tar.gz: from a postgres-pglite
#     checkout, built with build-pglite-standalone.sh
#   - pgdata.tar.gz: an initialized PGDATA, created with the JS build of PGlite
#     (the npm release PGLITE_VERSION, from pglite.env)
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
POSTGRES_PGLITE=${POSTGRES_PGLITE:-"$HERE/../pglite/postgres-pglite"}
OUT="$HERE/src/pytest_pglite/_artifacts"
RELEASE="$POSTGRES_PGLITE/dist/standalone/bin"
VERSION=$(source "$HERE/pglite.env" && echo "$PGLITE_VERSION")

mkdir -p "$OUT"
cp "$RELEASE/pglite-standalone.wasm" "$RELEASE/pglite-standalone-fs.tar.gz" "$OUT/"

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
postgres_pglite_commit=$(git -C "$POSTGRES_PGLITE" rev-parse HEAD)
pgdata_npm_version=@electric-sql/pglite@$VERSION
END
ls -la "$OUT"
