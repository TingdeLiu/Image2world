import fs from 'fs'
import path from 'path'

/**
 * Ask the backend where a person could actually stand, and write the answer
 * beside the collider.
 *
 * This runs as its own pass rather than inside the collider build, because a
 * video-reconstructed world's metric scale is calculated *from* the collider's
 * floor height — the scale the grid has to be measured at does not exist until
 * the collision mesh is finished. Taking the finished GLB also means nothing is
 * voxelised twice: the surface split is already in the file.
 *
 * Optional in the same way the collider is. A world without this map loses its
 * minimap, which is a real loss but not one worth failing a generation over,
 * and a collider built before the surface split has no floor to map at all.
 */
export interface WalkableMapOptions {
  /** Which world version to read and write. Defaults to 0. */
  index?: number
  /** SHARP worlds are upside down in viewer space. Ignored when `rotationX` is set. */
  flipY?: boolean
  /** Nerfstudio worlds are Z-up and pass an explicit angle instead. */
  rotationX?: number
  /** Reconstruction units per metre. The grid's thresholds are in metres. */
  metricScale?: number
  /** Viewer-space [x, z] to flood from. Without it, the largest region wins. */
  spawn?: [number, number]
  signal?: AbortSignal
  timeoutMs?: number
}

export interface WalkableMapMeta {
  cell_size: number
  cols: number
  rows: number
  origin: [number, number]
  standable_area: number
  walkable_area: number
  metric_scale_factor: number
}

export async function buildWalkableMap(
  backendUrl: string,
  worldOutDir: string,
  options: WalkableMapOptions = {},
): Promise<WalkableMapMeta | undefined> {
  const index = options.index ?? 0
  const glbPath = path.join(worldOutDir, `${index}-world.glb`)
  if (!fs.existsSync(glbPath)) return undefined

  try {
    const formData = new FormData()
    formData.append('file', new Blob([new Uint8Array(fs.readFileSync(glbPath))]), 'collider.glb')
    formData.append('flip_y', String(options.flipY ?? true))
    if (options.rotationX !== undefined) formData.append('rotation_x', String(options.rotationX))
    formData.append('metric_scale', String(options.metricScale ?? 1))
    if (options.spawn) {
      formData.append('spawn_x', String(options.spawn[0]))
      formData.append('spawn_z', String(options.spawn[1]))
    }

    const response = await fetch(`${backendUrl}/api/walkable`, {
      method: 'POST',
      body: formData,
      signal: options.signal ?? AbortSignal.timeout(options.timeoutMs ?? 180_000),
    })
    if (!response.ok) throw new Error((await response.text()).trim() || `HTTP ${response.status}`)

    const { mask_png: maskPng, ...meta } = await response.json() as
      { mask_png?: string } & WalkableMapMeta
    if (!maskPng) throw new Error('backend returned no walkable mask')

    fs.writeFileSync(path.join(worldOutDir, `${index}-world-walkable.png`), Buffer.from(maskPng, 'base64'))
    fs.writeFileSync(
      path.join(worldOutDir, `${index}-world-walkable.json`),
      `${JSON.stringify(meta, null, 2)}\n`,
    )
    console.log(
      `[Pipeline] Walkable map written: ${meta.walkable_area} m2 reachable of ${meta.standable_area} m2 standable.`,
    )
    return meta
  } catch (error) {
    if (options.signal?.aborted) throw error
    console.warn('[Pipeline] Walkable map unavailable; this world will have no minimap:', error)
    return undefined
  }
}
