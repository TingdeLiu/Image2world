import fs from 'fs'
import path from 'path'

/**
 * The splat levels a world publishes, coarsest last. `full_res` is the
 * reconstruction itself; the rest are decimated copies.
 */
export const SPLAT_LEVELS = ['full_res', '500k', '150k', '100k'] as const
export type SplatLevel = (typeof SPLAT_LEVELS)[number]

/**
 * Re-encode a gaussian-splat PLY as SPZ.
 *
 * Both formats hold the same splats, but a PLY stores every field as a float32
 * (and, for nerfstudio output, three normals nothing reads) while SPZ quantises
 * and gzips. Measured on this project's own worlds that is 3.7x for a SHARP
 * reconstruction and 10.8x for a Splatfacto one, at a positional error of
 * 1/4096 of a unit -- a quarter of a millimetre at metric scale. Spark's
 * `SplatMesh` reads either, so this only changes how many bytes cross the wire.
 *
 * Returns `undefined` rather than throwing: a world that fails to compress is
 * still a world, and the caller keeps its PLY.
 */
export async function encodeSpz(ply: Buffer): Promise<Buffer | undefined> {
  try {
    const { transcodeSpz, SplatFileType } = await import('@sparkjsdev/spark')
    const { fileBytes } = await transcodeSpz({
      inputs: [{ fileBytes: new Uint8Array(ply), fileType: SplatFileType.PLY }],
    })
    return Buffer.from(fileBytes)
  } catch (error) {
    console.warn('[Pipeline] SPZ encoding failed; the PLY will be served as-is:', error)
    return undefined
  }
}

export interface SplatLevelFile {
  level: SplatLevel
  ply: Buffer
}

/**
 * Write the SPZ copy of each splat level beside its PLY, and drop the PLYs that
 * exist only to be downloaded.
 *
 * `full_res.ply` stays: it is the lossless original, `/api/extract-prop` cuts
 * props out of it at runtime, and `scripts/generate-ply-lods.mts` rebuilds the
 * decimated levels from it. The decimated PLYs have no reader once their SPZ
 * exists -- the collision mesh is built from `500k.ply` earlier in the same
 * pipeline run, so by the time this runs nothing needs them.
 *
 * A level whose encode fails keeps its PLY, so the world stays loadable.
 */
export async function writeSpzLevels(
  worldOutDir: string,
  index: number,
  levels: SplatLevelFile[],
): Promise<number> {
  let written = 0

  for (const { level, ply } of levels) {
    const spz = await encodeSpz(ply)
    if (!spz) continue

    fs.writeFileSync(path.join(worldOutDir, `${index}-world-${level}.spz`), spz)
    written += 1
    console.log(
      `[Pipeline] ${level}: ${(ply.length / 1e6).toFixed(1)} MB PLY -> ` +
      `${(spz.length / 1e6).toFixed(1)} MB SPZ (${(ply.length / spz.length).toFixed(1)}x).`,
    )

    if (level === 'full_res') continue
    const plyPath = path.join(worldOutDir, `${index}-world-${level}.ply`)
    if (fs.existsSync(plyPath)) fs.rmSync(plyPath)
  }

  return written
}
