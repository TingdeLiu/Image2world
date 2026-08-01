import fs from 'node:fs'
import path from 'node:path'
import { createBinaryPlyLod } from '../src/utils/plyLod.ts'

const LOD_TARGETS = [
  ['500k', 500_000],
  ['150k', 150_000],
  ['100k', 100_000],
] as const

const worldsDirectory = path.join(process.cwd(), 'public', 'worlds')

if (!fs.existsSync(worldsDirectory)) {
  console.log(`No worlds directory found at ${worldsDirectory}`)
  process.exit(0)
}

let written = 0
let skipped = 0

for (const worldName of fs.readdirSync(worldsDirectory)) {
  if (worldName.startsWith('.')) continue

  const outputDirectory = path.join(worldsDirectory, worldName, 'output', 'world')
  if (!fs.existsSync(outputDirectory)) continue

  const fullResolutionFiles = fs
    .readdirSync(outputDirectory)
    .filter((fileName) => /^\d+-world-full_res\.ply$/i.test(fileName))

  for (const fileName of fullResolutionFiles) {
    const inputPath = path.join(outputDirectory, fileName)
    const input = fs.readFileSync(inputPath)
    const prefix = fileName.replace(/full_res\.ply$/i, '')

    for (const [label, vertexCount] of LOD_TARGETS) {
      const outputPath = path.join(outputDirectory, `${prefix}${label}.ply`)
      if (fs.existsSync(outputPath)) {
        skipped += 1
        continue
      }

      const lod = createBinaryPlyLod(input, vertexCount)
      if (!lod) {
        console.warn(`Skipping unsupported PLY: ${inputPath}`)
        skipped += 1
        break
      }

      fs.writeFileSync(outputPath, lod)
      written += 1
      console.log(`${worldName}: wrote ${path.basename(outputPath)} (${(lod.length / 1024 / 1024).toFixed(1)} MB)`)
    }
  }
}

console.log(`PLY LOD backfill complete: ${written} written, ${skipped} skipped.`)
