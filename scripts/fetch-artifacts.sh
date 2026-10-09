#!/bin/bash
# Collect the standalone PGlite build artifacts into src/pytest_pglite/_artifacts:
#   - pglite-standalone.wasm, pglite-standalone-fs.tar.gz: from a postgres-pglite
#     checkout, built with build-pglite-standalone.sh
#   - pgdata.tar.gz: an initialized PGDATA, created by running initdb-standalone.wasm
#     from the same build (see src/pytest_pglite/_initdb.py)
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
POSTGRES_PGLITE=${POSTGRES_PGLITE:-"$HERE/../pglite/postgres-pglite"}
OUT="$HERE/src/pytest_pglite/_artifacts"
RELEASE="$POSTGRES_PGLITE/dist/standalone/bin"

mkdir -p "$OUT"
rm -f "$OUT/pgdata.tar.gz"
cp "$RELEASE/pglite-standalone.wasm" "$RELEASE/pglite-standalone-fs.tar.gz" "$OUT/"
PYTEST_PGLITE_ARTIFACTS="$OUT" uv run --project "$HERE" --locked --no-dev \
    python -m pytest_pglite._initdb "$RELEASE/initdb-standalone.wasm" "$OUT/pgdata.tar.gz"
# provenance of the artifacts
cat > "$OUT/BUILD_INFO" <<END
postgres_pglite_commit=$(git -C "$POSTGRES_PGLITE" rev-parse HEAD)
END
ls -la "$OUT"
