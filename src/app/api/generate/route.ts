import { NextRequest, NextResponse } from 'next/server'
import fs from 'fs'
import path from 'path'
import { createBinaryPlyLod } from '../../../utils/plyLod'

export const dynamic = 'force-dynamic'
// Extend timeout to 5 minutes to accommodate sequential heavy GPU tasks (TripoSR, SAM2, AudioLDM)
export const maxDuration = 300

const AI_BACKEND_URL = (process.env.IMAGEWORLD_BACKEND_URL || 'http://localhost:8000').replace(/\/+$/, '')
const MAX_IMAGE_BYTES = 10 * 1024 * 1024
const ACCEPTED_IMAGE_TYPES = new Set(['image/jpeg', 'image/png', 'image/webp'])
const PLY_LOD_TARGETS = [
  ['500k', 500_000],
  ['150k', 150_000],
  ['100k', 100_000],
] as const
const BACKEND_TIMEOUTS = {
  segment: 120_000,
  crop: 45_000,
  imageTo3d: 180_000,
  sfx: 120_000,
  inpaint: 90_000,
  splat: 240_000,
  collider: 120_000,
} as const

type GenerationStage =
  | 'initializing'
  | 'segmenting'
  | 'objects'
  | 'inpainting'
  | 'splat'
  | 'finalizing'

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

// Segmentation backend: "sam2" (default, label-free) or "sam3" (concept prompts
// with semantic labels). Flip via the IMAGEWORLD_SEGMENTER env var; the backend
// degrades to SAM 2 automatically if SAM 3 isn't installed.
const SEGMENTER = (process.env.IMAGEWORLD_SEGMENTER || 'sam2').toLowerCase()

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

async function apiSegment(imageBuffer: Buffer, signal: AbortSignal, concepts?: string) {
  const formData = new FormData()
  formData.append('file', new Blob([new Uint8Array(imageBuffer)], { type: 'image/png' }), 'image.png')
  // Explicit concepts (guided segmentation) require SAM 3, so force it on.
  formData.append('segmenter', concepts ? 'sam3' : SEGMENTER)
  if (concepts) formData.append('concepts', concepts)
  const res = await backendRequest('/api/segment', {
    method: 'POST',
    body: formData,
  }, signal, BACKEND_TIMEOUTS.segment, 'segmenting', 'Segmentation')
  return res.json()
}

async function apiCrop(imageBuffer: Buffer, maskBuffer: Buffer, signal: AbortSignal) {
  const formData = new FormData()
  formData.append('image', new Blob([new Uint8Array(imageBuffer)], { type: 'image/png' }), 'image.png')
  formData.append('mask', new Blob([new Uint8Array(maskBuffer)], { type: 'image/png' }), 'mask.png')
  const res = await backendRequest('/api/crop', {
    method: 'POST',
    body: formData,
  }, signal, BACKEND_TIMEOUTS.crop, 'objects', 'Object crop')
  return Buffer.from(await res.arrayBuffer())
}

async function apiImageTo3D(croppedBuffer: Buffer, signal: AbortSignal) {
  const formData = new FormData()
  formData.append('file', new Blob([new Uint8Array(croppedBuffer)], { type: 'image/png' }), 'file.png')
  const res = await backendRequest('/api/image-to-3d', {
    method: 'POST',
    body: formData,
  }, signal, BACKEND_TIMEOUTS.imageTo3d, 'objects', '3D object generation')
  return Buffer.from(await res.arrayBuffer())
}

async function apiGenerateSfx(prompt: string, signal: AbortSignal) {
  const formData = new FormData()
  formData.append('prompt', prompt)
  formData.append('duration', '3.0')
  const res = await backendRequest('/api/generate-sfx', {
    method: 'POST',
    body: formData,
  }, signal, BACKEND_TIMEOUTS.sfx, 'objects', 'Sound generation')
  return Buffer.from(await res.arrayBuffer())
}

async function apiInpaint(imageBuffer: Buffer, maskBuffer: Buffer, signal: AbortSignal) {
  const formData = new FormData()
  formData.append('image', new Blob([new Uint8Array(imageBuffer)], { type: 'image/png' }), 'image.png')
  formData.append('mask', new Blob([new Uint8Array(maskBuffer)], { type: 'image/png' }), 'mask.png')
  const res = await backendRequest('/api/inpaint', {
    method: 'POST',
    body: formData,
  }, signal, BACKEND_TIMEOUTS.inpaint, 'inpainting', 'Background inpainting')
  return Buffer.from(await res.arrayBuffer())
}

async function apiImageToSplat(imageBuffer: Buffer, signal: AbortSignal) {
  const formData = new FormData()
  formData.append('file', new Blob([new Uint8Array(imageBuffer)], { type: 'image/png' }), 'image.png')
  const res = await backendRequest('/api/image-to-splat', {
    method: 'POST',
    body: formData,
  }, signal, BACKEND_TIMEOUTS.splat, 'splat', 'Background splat generation')
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

type ObjMask = { mask: string; label?: string | null; area?: number }

interface GenerationInput {
  file: File
  name: string
  concepts?: string
  selectedMasks?: ObjMask[]
}

/**
 * Turn the background splat into a collision mesh, and find the floor height
 * that lines the flat ground collider up with it.
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
    const data = await res.json() as { collider_glb?: string; ground_plane_offset?: number; face_count?: number }
    if (!data.collider_glb) throw new Error('backend returned no collider mesh')

    fs.writeFileSync(path.join(worldOutDir, '0-world.glb'), Buffer.from(data.collider_glb, 'base64'))
    const groundPlaneOffset = typeof data.ground_plane_offset === 'number' ? data.ground_plane_offset : 0
    console.log(`[Pipeline] Collision mesh written (${data.face_count ?? '?'} faces, ground offset ${groundPlaneOffset.toFixed(3)}).`)
    return { groundPlaneOffset }
  } catch (error) {
    if (signal.aborted) throw error
    console.warn('[Pipeline] Collision mesh unavailable; the world will have no wall collisions:', error)
    return undefined
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

async function runGeneration(
  { file, name, concepts, selectedMasks }: GenerationInput,
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
      progress: 2,
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

    // Ensure output directories exist
    fs.mkdirSync(path.join(worldDir, 'source'), { recursive: true })
    fs.mkdirSync(path.join(worldDir, 'output', 'world'), { recursive: true })
    fs.mkdirSync(path.join(worldDir, 'output', 'sfx'), { recursive: true })

    const imageArrayBuffer = await file.arrayBuffer()
    const imageBuffer = Buffer.from(imageArrayBuffer)

    // Save source image
    fs.writeFileSync(path.join(worldDir, 'source', '0-source.png'), imageBuffer)

    // 2. Write project.json
    const projectJson = {
      slug,
      display_name: name,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      notes: 'Generated with the local ImageWorld pipeline'
    }
    fs.writeFileSync(path.join(worldDir, 'project.json'), JSON.stringify(projectJson, null, 2))

    // 3. Decide which object masks to turn into 3D props:
    //    (a) user click-selected masks, (b) SAM 3 concept-guided, or (c) auto top-5.
    let sortedObjects: ObjMask[]
    if (selectedMasks) {
      report({
        stage: 'segmenting',
        progress: 8,
        message: 'Preparing selected object masks',
        detail: `${selectedMasks.length} hand-picked object${selectedMasks.length === 1 ? '' : 's'}`,
      })
      sortedObjects = selectedMasks
      console.log(`[Pipeline] Using ${sortedObjects.length} user-selected (click) masks.`)
    } else {
      report({
        stage: 'segmenting',
        progress: 8,
        message: concepts ? 'Segmenting requested objects' : 'Detecting foreground objects',
        detail: concepts ? `SAM 3 concepts: ${concepts}` : 'Scanning for the largest movable objects',
      })
      console.log(`[Pipeline] Segmenting image for world: ${name}${concepts ? ` (concepts: ${concepts})` : ''}`)
      const segmentData = await apiSegment(imageBuffer, signal, concepts)
      const objects = segmentData.objects || []
      console.log(`[Pipeline] Detected ${objects.length} candidate foreground objects.`)
      // Limit to top 5 largest objects to manage VRAM and generation time
      sortedObjects = [...objects]
        .sort((a, b) => (b.area || 0) - (a.area || 0))
        .slice(0, 5)
    }

    type PlacementInstance = {
      instanceId: string
      objectId: string
      assetId: string
      physics: 'rigidbody' | 'static' | 'ghost'
      position: [number, number, number]
      rotation: [number, number, number]
      scale: [number, number, number]
    }
    const instances: PlacementInstance[] = []

    // 4. Process each object segment
    for (let i = 0; i < sortedObjects.length; i++) {
      signal.throwIfAborted()
      const obj = sortedObjects[i]
      // SAM 3 attaches a semantic label (e.g. "chair"); SAM 2 returns null.
      // Use it for a human-friendly id, display name, and SFX prompt.
      const rawLabel = typeof obj.label === 'string' ? obj.label.trim() : ''
      const labelSlug = rawLabel.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '')
      const objectId = labelSlug ? `${labelSlug}-${i}` : `object_${i}`
      const displayName = rawLabel || `object ${i}`
      const objOutputDir = path.join(worldDir, 'output', objectId)
      fs.mkdirSync(objOutputDir, { recursive: true })
      fs.mkdirSync(path.join(objOutputDir, 'sfx'), { recursive: true })

      console.log(`[Pipeline] Processing ${objectId} (label: ${displayName}, area: ${obj.area})`)
      const objectProgressStart = 16 + (i / Math.max(sortedObjects.length, 1)) * 44
      const objectProgressUnit = 44 / Math.max(sortedObjects.length, 1)

      const maskBuffer = Buffer.from(obj.mask, 'base64')

      // A. Crop transparent object out
      report({
        stage: 'objects',
        progress: Math.round(objectProgressStart),
        message: `Extracting ${displayName}`,
        detail: `Object ${i + 1} of ${sortedObjects.length}`,
      })
      const croppedBuffer = await apiCrop(imageBuffer, maskBuffer, signal)
      fs.writeFileSync(path.join(objOutputDir, `0-${objectId}.png`), croppedBuffer)
      fs.writeFileSync(path.join(objOutputDir, `0-${objectId}-thumbnail.png`), croppedBuffer)

      // B. Generate 3D GLB (TripoSR)
      let meshReady = false
      try {
        report({
          stage: 'objects',
          progress: Math.round(objectProgressStart + objectProgressUnit * 0.25),
          message: `Building ${displayName} mesh`,
          detail: `Object ${i + 1} of ${sortedObjects.length} · TripoSR`,
        })
        const glbBuffer = await apiImageTo3D(croppedBuffer, signal)
        fs.writeFileSync(path.join(objOutputDir, `0-${objectId}.glb`), glbBuffer)
        meshReady = true
      } catch (err) {
        if (signal.aborted) throw new GenerationCanceledError()
        console.error(`[Pipeline] Failed to generate 3D mesh for ${objectId}:`, err)
      }

      // C. Generate SFX (AudioLDM-S)
      try {
        // A semantic label (SAM 3) yields a far better Foley prompt than "object 0".
        const prompt = `${displayName} collision bump impact sound effect, foley`
        report({
          stage: 'objects',
          progress: Math.round(objectProgressStart + objectProgressUnit * 0.72),
          message: `Synthesizing ${displayName} sound`,
          detail: `Object ${i + 1} of ${sortedObjects.length} · AudioLDM`,
        })
        const wavBuffer = await apiGenerateSfx(prompt, signal)
        fs.writeFileSync(path.join(objOutputDir, 'sfx', `0-sfx.wav`), wavBuffer)
      } catch (err) {
        if (signal.aborted) throw new GenerationCanceledError()
        console.error(`[Pipeline] Failed to generate SFX for ${objectId}:`, err)
      }

      // D. Save object.json descriptor
      const objectJson = {
        name: displayName,
        object: {
          name: displayName
        }
      }
      fs.writeFileSync(path.join(objOutputDir, 'object.json'), JSON.stringify(objectJson, null, 2))

      // E. Add placement entry
      const x = (i - (sortedObjects.length - 1) / 2) * 1.5
      const y = 0.5
      const z = -2.5
      if (meshReady) {
        instances.push({
          instanceId: `instance_${objectId}`,
          objectId: objectId,
          assetId: `${slug}/${objectId}/0`,
          physics: 'rigidbody',
          position: [x, y, z],
          rotation: [0, 0, 0],
          scale: [1, 1, 1]
        })
      }
    }

    // 5. Sequential Inpainting to produce Clean Plate (LaMa)
    console.log(`[Pipeline] Starting sequential background inpainting...`)
    let currentImageBuffer = imageBuffer
    for (let i = 0; i < sortedObjects.length; i++) {
      signal.throwIfAborted()
      const obj = sortedObjects[i]
      const maskBuffer = Buffer.from(obj.mask, 'base64')
      try {
        report({
          stage: 'inpainting',
          progress: Math.round(62 + ((i + 1) / Math.max(sortedObjects.length, 1)) * 12),
          message: 'Cleaning the background plate',
          detail: `Removing object ${i + 1} of ${sortedObjects.length} · LaMa`,
        })
        currentImageBuffer = await apiInpaint(currentImageBuffer, maskBuffer, signal)
      } catch (err) {
        if (signal.aborted) throw new GenerationCanceledError()
        console.error(`[Pipeline] Failed inpainting step ${i}:`, err)
      }
    }
    fs.writeFileSync(path.join(worldDir, 'output', 'world', '0-world-plate.jpg'), currentImageBuffer)

    // 6. Generate the background 3D Gaussian splat from the clean plate (Apple SHARP).
    //    The clean plate has foreground objects removed, so the splat is a clean
    //    backdrop while the props are rendered separately as interactive meshes.
    //    Falls back to the static home-room template if SHARP is unavailable.
    const worldOutDir = path.join(worldDir, 'output', 'world')
    let backgroundReady = false
    let groundPlaneOffset = 0
    try {
      report({
        stage: 'splat',
        progress: 78,
        message: 'Generating navigable world splat',
        detail: 'Reconstructing the clean plate with SHARP',
      })
      console.log(`[Pipeline] Generating background splat via SHARP...`)
      const plyBuffer = await apiImageToSplat(currentImageBuffer, signal)
      fs.writeFileSync(path.join(worldOutDir, '0-world-full_res.ply'), plyBuffer)
      report({
        stage: 'splat',
        progress: 91,
        message: 'Building point-cloud detail levels',
        detail: 'Preparing 100k, 150k, and 500k variants',
      })
      for (const [label, vertexCount] of PLY_LOD_TARGETS) {
        const lodBuffer = createBinaryPlyLod(plyBuffer, vertexCount)
        if (lodBuffer && lodBuffer.length < plyBuffer.length) {
          fs.writeFileSync(path.join(worldOutDir, `0-world-${label}.ply`), lodBuffer)
        }
      }
      // SHARP only produces Gaussians. Without a collision mesh the character
      // controller has nothing but the flat ground plane to stand on, and a USD
      // export carries no room geometry. Derive one from the splat cloud; the
      // same pass finds the floor height that aligns the ground plane.
      const collider = await buildBackgroundCollider(worldOutDir, signal)

      // Minimal world manifest so the scanner picks up the local .ply splat.
      const worldJson = {
        world_id: slug,
        display_name: name,
        assets: {
          splats: {
            spz_urls: { full_res: '' },
            semantics_metadata: {
              metric_scale_factor: 1,
              ground_plane_offset: collider?.groundPlaneOffset ?? 0,
              flip_y: true,
            },
          },
        },
      }
      fs.writeFileSync(path.join(worldOutDir, '0-world.json'), JSON.stringify(worldJson, null, 2))
      groundPlaneOffset = collider?.groundPlaneOffset ?? 0
      backgroundReady = true
      console.log(`[Pipeline] SHARP background splat written (${plyBuffer.length} bytes).`)
    } catch (err) {
      if (signal.aborted) throw new GenerationCanceledError()
      console.error('[Pipeline] SHARP background reconstruction failed:', err)
    }

    if (!backgroundReady) {
      // This used to copy another world's splat in as a "fallback template",
      // which silently produced a world showing somebody else's room while
      // reporting success. Failing loudly is the honest behaviour: the caller
      // sees a retryable error and the staged directory is cleaned up.
      throw new GenerationPipelineError(
        'Background reconstruction failed. Check that SHARP is installed (backend/README.md step 6) and the AI backend is running.',
        'background_failed',
        'splat',
        true,
      )
    }

    // Global ambience, shared by every world.
    const ambienceDir = path.join(process.cwd(), 'public', 'assets', 'ambience')
    if (fs.existsSync(ambienceDir)) {
      copyRecursiveSync(ambienceDir, path.join(worldDir, 'output', 'sfx'))
    }

    // 7. Write scene.json
    report({
      stage: 'finalizing',
      progress: 97,
      message: 'Finalizing world configuration',
      detail: `${instances.length} interactive object${instances.length === 1 ? '' : 's'} ready`,
    })
    const sceneJson = {
      version: 1,
      instances,
      sun: {
        intensity: 1,
        rotation: [0, 0, 0],
        environmentIntensity: 1
      },
      metricScaleFactor: 1,
      // scene.json wins over the world manifest in both the viewer and the USD
      // exporter, so it has to carry the calibrated value too.
      groundPlaneOffset,
      groundPlaneColliderEnabled: true
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

function parseSelectedMasks(value: FormDataEntryValue | null): ObjMask[] | undefined {
  if (typeof value !== 'string') return undefined
  try {
    const parsed = JSON.parse(value)
    if (!Array.isArray(parsed)) return []
    return parsed
      .filter((mask): mask is ObjMask => Boolean(mask && typeof mask.mask === 'string' && mask.mask.length > 0))
      .slice(0, 8)
  } catch {
    return []
  }
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
  const concepts = (formData.get('concepts') as string | null)?.trim() || undefined
  const masksValue = formData.get('masks')
  const selectedMasks = parseSelectedMasks(masksValue)

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
  if (masksValue !== null && selectedMasks?.length === 0) {
    return generationErrorResponse('The selected object masks are invalid. Select the objects again.', 400)
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
        { file, name, concepts, selectedMasks },
        (progress) => send({ type: 'progress', ...progress }),
        generationController.signal,
      )
        .then(({ slug }) => {
          send({ type: 'complete', success: true, slug })
        })
        .catch((error: unknown) => {
          const pipelineError = error instanceof GenerationPipelineError ? error : undefined
          const canceled = error instanceof GenerationCanceledError
            || (error instanceof DOMException && error.name === 'AbortError')
          send({
            type: 'error',
            error: {
              code: canceled ? 'generation_canceled' : pipelineError?.code || 'generation_failed',
              message: canceled
                ? 'Generation canceled. No world was published.'
                : error instanceof Error
                  ? error.message
                  : 'Generation failed.',
              stage: pipelineError?.stage || 'finalizing',
              retryable: canceled || pipelineError?.retryable !== false,
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
