import fs from 'node:fs'
import path from 'node:path'
import { encodeSpz, SPLAT_LEVELS } from '../src/utils/spz.ts'

/**
 * Backfill: give every world that predates SPZ compression the same compressed
 * splats the pipeline now writes for new ones.
 *
 * Adding files is safe, so that happens by default. Removing the decimated PLYs
 * they replace is not quite -- nothing reads them once an .spz exists, but they
 * are the user's own generated data -- so `--prune` asks for it explicitly.
 * `npm run assets:lod` rebuilds them from `full_res.ply`, which is never
 * pruned: it is the lossless original that `/api/extract-prop` cuts props from.
 */
const prune = process.argv.includes('--prune')
const worldsDirectory = path.join(process.cwd(), 'public', 'worlds')

if (!fs.existsSync(worldsDirectory)) {
  console.log(`No worlds directory found at ${worldsDirectory}`)
  process.exit(0)
}

const megabytes = (bytes: number) => `${(bytes / 1e6).toFixed(1)} MB`

let written = 0
let skipped = 0
let failed = 0
let plyBytes = 0
let spzBytes = 0
let prunable = 0

for (const worldName of fs.readdirSync(worldsDirectory)) {
  if (worldName.startsWith('.')) continue

  const outputDirectory = path.join(worldsDirectory, worldName, 'output', 'world')
  if (!fs.existsSync(outputDirectory)) continue

  const levelPattern = new RegExp(`^(\\d+)-world-(${SPLAT_LEVELS.join('|')})\\.ply$`, 'i')
  const plyFiles = fs.readdirSync(outputDirectory).filter((name) => levelPattern.test(name))

  for (const fileName of plyFiles) {
    const [, index, level] = fileName.match(levelPattern)!
    const plyPath = path.join(outputDirectory, fileName)
    const spzPath = path.join(outputDirectory, `${index}-world-${level}.spz`)

    if (fs.existsSync(spzPath)) {
      skipped += 1
    } else {
      const ply = fs.readFileSync(plyPath)
      const spz = await encodeSpz(ply)
      if (!spz) {
        console.warn(`${worldName}: could not compress ${fileName}`)
        failed += 1
        continue
      }

      fs.writeFileSync(spzPath, spz)
      written += 1
      plyBytes += ply.length
      spzBytes += spz.length
      console.log(
        `${worldName}: ${fileName} ${megabytes(ply.length)} -> ` +
        `${path.basename(spzPath)} ${megabytes(spz.length)} (${(ply.length / spz.length).toFixed(1)}x)`,
      )
    }

    if (level.toLowerCase() === 'full_res') continue
    const reclaimable = fs.statSync(plyPath).size
    if (prune) {
      fs.rmSync(plyPath)
      console.log(`${worldName}: pruned ${fileName} (${megabytes(reclaimable)})`)
    }
    prunable += reclaimable
  }
}

console.log(
  `\nSPZ backfill complete: ${written} written, ${skipped} already present, ${failed} failed.`,
)
if (written) {
  console.log(`Compressed ${megabytes(plyBytes)} of PLY into ${megabytes(spzBytes)} of SPZ.`)
}
if (prunable) {
  console.log(
    prune
      ? `Pruned ${megabytes(prunable)} of superseded detail-level PLYs.`
      : `${megabytes(prunable)} of superseded detail-level PLYs can be removed with --prune.`,
  )
}
