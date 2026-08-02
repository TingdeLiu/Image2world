import { NextRequest, NextResponse } from 'next/server'
import fs from 'fs'
import path from 'path'
import { createBinaryPlyLod } from '../../../utils/plyLod'
import { MarbleError, generateWorldWithMarble } from './marble'

export const dynamic = 'force-dynamic'
// The local SHARP path finishes in well under a minute, but a Marble generation
// routinely runs eight minutes or more.
export const maxDuration = 900

const AI_BACKEND_URL = (process.env.IMAGEWORLD_BACKEND_URL || 'http://localhost:8000').replace(/\/+$/, '')
const MAX_IMAGE_BYTES = 10 * 1024 * 1024
const ACCEPTED_IMAGE_TYPES = new Set(['image/jpeg', 'image/png', 'image/webp'])
const PLY_LOD_TARGETS = [
  ['500k', 500_000],
  ['150k', 150_000],
  ['100k', 100_000],
] as const
const BACKEND_TIMEOUTS = {
  splat: 240_000,
  collider: 120_000,
} as const

type GenerationStage = 'initializing' | 'splat' | 'finalizing'

interface GenerationProgress {
  type: 'progress'
  stage: GenerationStage
  progress: number
  message: string
  detail?: string
}

type ProgressReporter = (progress: Omit<GenerationProgress, 'type'>) => void

class GenerationPipelineError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly stage: GenerationStage,
    readonly retryable = true,
  ) {
    super(message)
    this.name = 'GenerationPipelineError'
  }
}

class GenerationCanceledError extends Error {
  constructor() {
    super('Generation canceled. No world was published.')
    this.name = 'AbortError'
  }
}

function backendUrl(pathname: string) {
  return `${AI_BACKEND_URL}${pathname}`
}

async function backendRequest(
  pathname: string,
  init: RequestInit,
  signal: AbortSignal,
  timeoutMs: number,
  stage: GenerationStage,
  label: string,
) {
  const timeoutSignal = AbortSignal.timeout(timeoutMs)
  const combinedSignal = AbortSignal.any([signal, timeoutSignal])

  try {
    const response = await fetch(backendUrl(pathname), { ...init, signal: combinedSignal })
    if (!response.ok) {
      const detail = (await response.text()).trim()
      throw new GenerationPipelineError(
        `${label} failed${detail ? `: ${detail}` : ` with HTTP ${response.status}`}`,
        'backend_error',
        stage,
      )
    }
    return response
  } catch (error) {
    if (signal.aborted) throw new GenerationCanceledError()
    if (timeoutSignal.aborted) {
      throw new GenerationPipelineError(
        `${label} timed out after ${Math.round(timeoutMs / 1000)} seconds.`,
        'backend_timeout',
        stage,
      )
    }
    if (error instanceof GenerationPipelineError) throw error
    throw new GenerationPipelineError(
      `${label} could not reach the local AI backend at ${AI_BACKEND_URL}.`,
      'backend_unavailable',
      stage,
    )
  }
}

async function apiImageToSplat(imageBuffer: Buffer, signal: AbortSignal) {
  const formData = new FormData()
  formData.append('file', new Blob([new Uint8Array(imageBuffer)], { type: 'image/png' }), 'image.png')
  const res = await backendRequest('/api/image-to-splat', {
    method: 'POST',
    body: formData,
  }, signal, BACKEND_TIMEOUTS.splat, 'splat', 'World reconstruction')
  return Buffer.from(await res.arrayBuffer())
}

function copyRecursiveSync(src: string, dest: string) {
  const exists = fs.existsSync(src)
  const stats = exists && fs.statSync(src)
  const isDirectory = stats && stats.isDirectory()
  if (isDirectory) {
    fs.mkdirSync(dest, { recursive: true })
    fs.readdirSync(src).forEach((childItemName) => {
      copyRecursiveSync(path.join(src, childItemName), path.join(dest, childItemName))
    })
  } else {
    fs.copyFileSync(src, dest)
  }
}

function createWorldSlug(name: string, worldsDir: string, stagingDir: string) {
  const base = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/(^-|-$)/g, '') || 'world'

  for (let attempt = 0; attempt < 10; attempt += 1) {
    const suffix = Math.random().toString(36).slice(2, 8)
    const slug = `${base}-${suffix}`
    if (!fs.existsSync(path.join(worldsDir, slug)) && !fs.existsSync(path.join(stagingDir, slug))) {
      return slug
    }
  }

  throw new Error('Could not allocate a unique world identifier')
}

export async function GET() {
  try {
    const response = await fetch(backendUrl('/'), {
      cache: 'no-store',
      signal: AbortSignal.timeout(3_000),
    })
    if (!response.ok) {
      return NextResponse.json(
        { ready: false, message: `AI backend returned HTTP ${response.status}` },
        { status: 503 },
      )
    }
    return NextResponse.json({ ready: true, backend: AI_BACKEND_URL })
  } catch {
    return NextResponse.json(
      {
        ready: false,
        message: `AI backend is unavailable at ${AI_BACKEND_URL}. Start backend/server.py and try again.`,
      },
      { status: 503 },
    )
  }
}

interface GenerationInput {
  file: File
  name: string
  /**
   * The user's own World Labs key. Present means "reconstruct with Marble",
   * absent means "use the local SHARP backend". It is never written to disk or
   * logged -- it lives in the caller's browser and only passes through here.
   */
  marbleApiKey?: string
}

/**
 * Turn the world splat into a collision mesh, and find the floor height that
 * lines the flat ground collider up with it.
 *
 * Optional by design: a world without a collider still loads and renders, it
 * just has no walls to bump into, so a failure here degrades instead of
 * sinking a generation that already succeeded.
 */
async function buildBackgroundCollider(worldOutDir: string, signal: AbortSignal) {
  // The 500k LOD is plenty for a 0.05 m voxel grid and uploads ~5x faster.
  const candidates = ['0-world-500k.ply', '0-world-full_res.ply']
    .map((name) => path.join(worldOutDir, name))
    .filter((candidate) => fs.existsSync(candidate))
  if (!candidates.length) return undefined

  try {
    const formData = new FormData()
    formData.append('file', new Blob([new Uint8Array(fs.readFileSync(candidates[0]))]), 'world.ply')
    const res = await backendRequest(
      '/api/splat-to-collider',
      { method: 'POST', body: formData },
      signal,
      BACKEND_TIMEOUTS.collider,
      'splat',
      'Collision mesh',
    )
    const data = await res.json() as {
      collider_glb?: string
      ground_plane_offset?: number
      face_count?: number
      bounds_viewer?: { min: number[]; max: number[] }
    }
    if (!data.collider_glb) throw new Error('backend returned no collider mesh')

    fs.writeFileSync(path.join(worldOutDir, '0-world.glb'), Buffer.from(data.collider_glb, 'base64'))
    const groundPlaneOffset = typeof data.ground_plane_offset === 'number' ? data.ground_plane_offset : 0
    console.log(`[Pipeline] Collision mesh written (${data.face_count ?? '?'} faces, ground offset ${groundPlaneOffset.toFixed(3)}).`)
    return { groundPlaneOffset, boundsViewer: data.bounds_viewer }
  } catch (error) {
    if (signal.aborted) throw error
    console.warn('[Pipeline] Collision mesh unavailable; the world will have no wall collisions:', error)
    return undefined
  }
}

/**
 * Pick a spawn point inside the reconstructed room.
 *
 * SHARP reconstructs from where the photo was taken, which is typically outside
 * the space itself -- measured on a real room, the geometry started 2.1 m in
 * front of the default spawn, so the player began outside the world looking in,
 * with nothing but the 200 m ground plane behind them. Stand them just inside
 * the near edge instead, centred on the room's width.
 */
function spawnInsideRoom(bounds?: { min: number[]; max: number[] }) {
  if (!bounds || bounds.min.length !== 3 || bounds.max.length !== 3) return undefined
  const [minX, , minZ] = bounds.min
  const [maxX, , maxZ] = bounds.max
  const depth = maxZ - minZ
  if (!Number.isFinite(depth) || depth <= 0) return undefined

  // Step in from the near (camera-facing) edge, but never past the midpoint of
  // a shallow room.
  const inset = Math.min(1.5, depth * 0.35)
  return {
    x: (minX + maxX) / 2,
    z: maxZ - inset,
  }
}

// Windows denies a directory rename while any process still holds a handle
// inside it -- a virus scanner or the search indexer working through the
// freshly written splat files is enough, and the world is hundreds of
// megabytes by this point. Retry, then fall back to copy + delete.
async function publishWorldDir(stagedDir: string, finalDir: string) {
  const retryDelaysMs = [100, 300, 700, 1500, 3000]
  const isTransient = (error: unknown) =>
    ['EPERM', 'EACCES', 'EBUSY', 'ENOTEMPTY'].includes((error as NodeJS.ErrnoException)?.code ?? '')

  for (let attempt = 0; attempt <= retryDelaysMs.length; attempt += 1) {
    try {
      fs.renameSync(stagedDir, finalDir)
      return
    } catch (error) {
      if (!isTransient(error) || attempt === retryDelaysMs.length) {
        if (!isTransient(error)) throw error
        break
      }
      await new Promise((resolve) => setTimeout(resolve, retryDelaysMs[attempt]))
    }
  }

  // A copy leaves the staged copy behind on failure, which the caller cleans up.
  fs.cpSync(stagedDir, finalDir, { recursive: true })
  fs.rmSync(stagedDir, { recursive: true, force: true })
}

/**
 * Reconstruct a navigable world from one photo.
 *
 * Two backends, chosen by whether the caller supplied a Marble key:
 *
 * - **Marble** (generative world model) invents what the camera never saw, so
 *   the room comes back sealed on every side. Costs money and takes minutes.
 * - **SHARP** (local, single-view) is free and finishes in seconds, but only
 *   reconstructs surfaces the camera actually saw -- measured on a real room,
 *   a third of the directions behind the viewer had no geometry at all.
 *
 * Either way the source image goes in untouched. An earlier version segmented
 * the foreground out, erased it with LaMa and rebuilt each object as a separate
 * TripoSR mesh -- that threw away the room's real furniture geometry and
 * replaced it with a single-view guess, while costing most of the runtime.
 */
async function runGeneration(
  { file, name, marbleApiKey }: GenerationInput,
  report: ProgressReporter,
  signal: AbortSignal,
) {
  const worldsDir = path.join(process.cwd(), 'public', 'worlds')
  const stagingDir = path.join(worldsDir, '.staging')
  let stagedWorldDir: string | undefined

  try {
    signal.throwIfAborted()
    report({
      stage: 'initializing',
      progress: 3,
      message: 'Initializing generation workspace',
      detail: 'Validating source image and reserving a world ID',
    })

    // 1. Generate into a hidden staging directory. Only publish the world after
    // every descriptor is written, so failed jobs never appear in the sidebar.
    fs.mkdirSync(stagingDir, { recursive: true })
    const slug = createWorldSlug(name, worldsDir, stagingDir)
    stagedWorldDir = path.join(stagingDir, slug)
    const worldDir = stagedWorldDir
    const finalWorldDir = path.join(worldsDir, slug)

    fs.mkdirSync(path.join(worldDir, 'source'), { recursive: true })
    const worldOutDir = path.join(worldDir, 'output', 'world')
    fs.mkdirSync(worldOutDir, { recursive: true })
    fs.mkdirSync(path.join(worldDir, 'output', 'sfx'), { recursive: true })

    const imageBuffer = Buffer.from(await file.arrayBuffer())
    fs.writeFileSync(path.join(worldDir, 'source', '0-source.png'), imageBuffer)

    // 2. Write project.json
    const projectJson = {
      slug,
      display_name: name,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      notes: marbleApiKey
        ? 'Generated with World Labs Marble (marble-1.1)'
        : 'Generated with the local ImageWorld pipeline (Apple SHARP)',
    }
    fs.writeFileSync(path.join(worldDir, 'project.json'), JSON.stringify(projectJson, null, 2))

    // 2b. Marble path: it returns a finished world, so the local reconstruction,
    // LOD and collider steps below are all skipped.
    if (marbleApiKey) {
      console.log('[Pipeline] Reconstructing world via World Labs Marble...')
      const semantics = await generateWorldWithMarble({
        apiKey: marbleApiKey,
        imageBuffer,
        displayName: name,
        slug,
        worldOutDir,
        report: (progress, message, detail) => report({ stage: 'splat', progress, message, detail }),
        signal,
      })

      const ambienceDir = path.join(process.cwd(), 'public', 'assets', 'ambience')
      if (fs.existsSync(ambienceDir)) {
        copyRecursiveSync(ambienceDir, path.join(worldDir, 'output', 'sfx'))
      }

      report({
        stage: 'finalizing',
        progress: 96,
        message: 'Finalizing world configuration',
        detail: 'Writing the scene descriptor',
      })
      // Marble centres the world on the viewpoint, which sits inside the space
      // it generates -- so unlike SHARP, the origin needs no correction.
      const marbleScene = {
        version: 1,
        instances: [],
        sun: { intensity: 1, rotation: [0, 0, 0], environmentIntensity: 1 },
        metricScaleFactor: semantics.metricScaleFactor,
        groundPlaneOffset: semantics.groundPlaneOffset,
        groundPlaneColliderEnabled: true,
      }
      fs.writeFileSync(path.join(worldDir, 'scene.json'), JSON.stringify(marbleScene, null, 2))

      await publishWorldDir(worldDir, finalWorldDir)
      stagedWorldDir = undefined
      console.log(`[Pipeline] Marble generation complete for slug: ${slug}`)
      return { slug }
    }

    // 3. Reconstruct the world as a 3D gaussian splat (Apple SHARP).
    report({
      stage: 'splat',
      progress: 12,
      message: 'Reconstructing the world',
      detail: 'Lifting the photo into 3D with SHARP',
    })
    console.log('[Pipeline] Reconstructing world splat via SHARP...')
    const plyBuffer = await apiImageToSplat(imageBuffer, signal)
    fs.writeFileSync(path.join(worldOutDir, '0-world-full_res.ply'), plyBuffer)
    console.log(`[Pipeline] SHARP world splat written (${plyBuffer.length} bytes).`)

    // 4. Detail levels, so the viewer can stream something light first.
    report({
      stage: 'splat',
      progress: 72,
      message: 'Building point-cloud detail levels',
      detail: 'Preparing 100k, 150k, and 500k variants',
    })
    for (const [label, vertexCount] of PLY_LOD_TARGETS) {
      const lodBuffer = createBinaryPlyLod(plyBuffer, vertexCount)
      if (lodBuffer && lodBuffer.length < plyBuffer.length) {
        fs.writeFileSync(path.join(worldOutDir, `0-world-${label}.ply`), lodBuffer)
      }
    }

    // 5. SHARP only produces gaussians. Without a collision mesh the character
    // controller has nothing but the flat ground plane to stand on, and a USD
    // export carries no room geometry. The same pass finds the floor height
    // that aligns the ground plane.
    report({
      stage: 'splat',
      progress: 86,
      message: 'Building collision geometry',
      detail: 'Deriving walkable surfaces from the point cloud',
    })
    const collider = await buildBackgroundCollider(worldOutDir, signal)
    const groundPlaneOffset = collider?.groundPlaneOffset ?? 0
    const spawn = spawnInsideRoom(collider?.boundsViewer)
    if (spawn) {
      console.log(`[Pipeline] Spawn placed inside the room at x=${spawn.x.toFixed(2)}, z=${spawn.z.toFixed(2)}.`)
    }

    // 6. Minimal world manifest so the scanner picks up the local .ply splat.
    const worldJson = {
      world_id: slug,
      display_name: name,
      assets: {
        splats: {
          spz_urls: { full_res: '' },
          semantics_metadata: {
            metric_scale_factor: 1,
            ground_plane_offset: groundPlaneOffset,
            flip_y: true,
          },
        },
      },
    }
    fs.writeFileSync(path.join(worldOutDir, '0-world.json'), JSON.stringify(worldJson, null, 2))

    // Global ambience, shared by every world.
    const ambienceDir = path.join(process.cwd(), 'public', 'assets', 'ambience')
    if (fs.existsSync(ambienceDir)) {
      copyRecursiveSync(ambienceDir, path.join(worldDir, 'output', 'sfx'))
    }

    // 7. Write scene.json. `instances` stays empty: the world is the scene, and
    // the placement editor can still add props from other worlds by hand.
    report({
      stage: 'finalizing',
      progress: 96,
      message: 'Finalizing world configuration',
      detail: 'Writing the scene descriptor',
    })
    const sceneJson = {
      version: 1,
      instances: [],
      sun: {
        intensity: 1,
        rotation: [0, 0, 0],
        environmentIntensity: 1,
      },
      metricScaleFactor: 1,
      // scene.json wins over the world manifest in both the viewer and the USD
      // exporter, so it has to carry the calibrated value too.
      groundPlaneOffset,
      groundPlaneColliderEnabled: true,
      ...(spawn ? { spawnPoint: [spawn.x, spawn.z] as [number, number] } : {}),
    }
    fs.writeFileSync(path.join(worldDir, 'scene.json'), JSON.stringify(sceneJson, null, 2))

    await publishWorldDir(worldDir, finalWorldDir)
    stagedWorldDir = undefined
    console.log(`[Pipeline] End-to-end generation complete for slug: ${slug}`)
    return { slug }

  } catch (error: unknown) {
    console.error('Failed to run online generation pipeline:', error)
    if (stagedWorldDir && fs.existsSync(stagedWorldDir)) {
      try {
        fs.rmSync(stagedWorldDir, { recursive: true, force: true })
      } catch (cleanupError) {
        console.error(`Could not clean failed staging world at ${stagedWorldDir}:`, cleanupError)
      }
    }
    throw error
  }
}

function generationErrorResponse(
  message: string,
  status: number,
  code = 'invalid_request',
) {
  return NextResponse.json(
    { error: { code, message, stage: 'initializing', retryable: false } },
    { status },
  )
}

export async function POST(request: NextRequest) {
  let formData: FormData
  try {
    formData = await request.formData()
  } catch {
    return generationErrorResponse('Could not read the submitted generation form.', 400)
  }

  const fileValue = formData.get('file')
  const file = fileValue instanceof File ? fileValue : null
  const name = (formData.get('name') as string | null)?.trim() || ''
  // Supplied per-request from the user's browser; never stored server-side.
  const marbleApiKey = (formData.get('marbleApiKey') as string | null)?.trim() || undefined

  if (!file || !name) {
    return generationErrorResponse('Missing image file or world name.', 400)
  }
  if (name.length > 80) {
    return generationErrorResponse('World name must be 80 characters or fewer.', 400)
  }
  if (!ACCEPTED_IMAGE_TYPES.has(file.type)) {
    return generationErrorResponse('Unsupported image type. Use PNG, JPG, or WEBP.', 415)
  }
  if (file.size > MAX_IMAGE_BYTES) {
    return generationErrorResponse('Image is too large. Maximum size is 10 MB.', 413)
  }

  const encoder = new TextEncoder()
  const generationController = new AbortController()
  const abortGeneration = () => generationController.abort()
  request.signal.addEventListener('abort', abortGeneration, { once: true })
  let streamClosed = false

  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const send = (event: object) => {
        if (streamClosed) return
        try {
          controller.enqueue(encoder.encode(`${JSON.stringify(event)}\n`))
        } catch {
          streamClosed = true
          generationController.abort()
        }
      }

      void runGeneration(
        { file, name, marbleApiKey },
        (progress) => send({ type: 'progress', ...progress }),
        generationController.signal,
      )
        .then(({ slug }) => {
          send({ type: 'complete', success: true, slug })
        })
        .catch((error: unknown) => {
          const pipelineError = error instanceof GenerationPipelineError ? error : undefined
          const marbleError = error instanceof MarbleError ? error : undefined
          const canceled = error instanceof GenerationCanceledError
            || (error instanceof DOMException && error.name === 'AbortError')
          send({
            type: 'error',
            error: {
              code: canceled
                ? 'generation_canceled'
                : marbleError
                  ? 'marble_error'
                  : pipelineError?.code || 'generation_failed',
              message: canceled
                ? 'Generation canceled. No world was published.'
                : error instanceof Error
                  ? error.message
                  : 'Generation failed.',
              stage: pipelineError?.stage || 'splat',
              retryable: canceled || (marbleError?.retryable ?? pipelineError?.retryable !== false),
            },
          })
        })
        .finally(() => {
          request.signal.removeEventListener('abort', abortGeneration)
          if (!streamClosed) {
            streamClosed = true
            controller.close()
          }
        })
    },
    cancel() {
      streamClosed = true
      generationController.abort()
    },
  })

  return new Response(stream, {
    headers: {
      'Content-Type': 'application/x-ndjson; charset=utf-8',
      'Cache-Control': 'no-store',
      'X-Accel-Buffering': 'no',
    },
  })
}
