// Create a pristine PGDATA with the regular (JS) PGlite build of the same sources.
//
// PGlite runs on a host directory (NodeFS) and is closed, which shuts Postgres
// down cleanly (shutdown checkpoint, no postmaster.pid), so the data dir starts
// without recovery. The caller tars up the directory.
import { PGlite } from '@electric-sql/pglite'

const [dataDir] = process.argv.slice(2)
const db = await PGlite.create(dataDir)
const { rows } = await db.query('SELECT version()')
await db.close()
console.log(`initialized ${dataDir}: ${rows[0].version}`)
