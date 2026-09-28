// Create a pristine, initialized PGDATA with the regular (JS) PGlite build of the
// same sources, and write it as a tarball with paths relative to PGDATA.
import { PGlite } from '@electric-sql/pglite'
import { writeFileSync } from 'node:fs'

const [out] = process.argv.slice(2)
const db = await PGlite.create()
// checkpoint so the data dir needs no WAL replay at startup
await db.exec('CHECKPOINT')
const blob = await db.dumpDataDir('gzip')
await db.close()
writeFileSync(out, Buffer.from(await blob.arrayBuffer()))
console.log(`wrote ${out} (${blob.size} bytes)`)
