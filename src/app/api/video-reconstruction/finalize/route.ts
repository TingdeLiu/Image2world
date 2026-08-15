import fs from 'fs'
import path from 'path'
import { NextRequest, NextResponse } from 'next/server'
import { createBinaryPlyLod } from '../../../../utils/plyLod'
import { isAppRequest } from '../../../../utils/sameOrigin'
import { buildWalkableMap } from '../../../../utils/walkableMap'

export const dynamic = 'force-dynamic'
export const maxDuration = 900

const AI_BACKEND_URL = (process.env.IMAGEWORLD_BACKEND_URL || 'http://localhost:8000').replace(/\/+$/, '')
const JOB_ROOT = path.resolve(
  process.env.IMAGEWORLD_RECONSTRUCTION_JOB_ROOT
    || path.join(process.cwd(), '.artifacts', 'reconstruction-jobs'),
)
const JOB_ID_PATTERN = /^[a-f0-9]{12}$/
const PLY_LOD_TARGETS = [
  ['500k', 500_000],
  ['150k', 150_000],
  ['100k', 100_000],
] as const
const NERFSTUDIO_ROTATION_X = -Math.PI / 2

interface ReconstructionJob {
  id: string
  name: string
  status: string
  quality: string
  output_ply?: string
  source_path?: string
  thumbnail_path?: string
  registered_frames?: number
  gaussian_count?: number
  camera_height?: number
  initial_view?: {
    position?: [number, number, number]
    yaw?: number
    pitch?: number
  }
}

function isInside(root: string, candidate: string) {
  const relative = path.relative(root, candidate)
  return relative !== '' && !relative.startsWith('..') && !path.isAbsolute(relative)
}

function safeJobArtifact(jobId: string, candidate: string | undefined) {
  if (!candidate) return undefined
  const jobDir = path.resolve(JOB_ROOT, jobId)
  const resolved = path.resolve(candidate)
  if (!isInside(jobDir, resolved)) throw new Error('Backend returned an unsafe job artifact path')
  if (!fs.existsSync(resolved) || !fs.statSync(resolved).isFile()) return undefined
  return resolved
}

function copyRecursiveSync(source: string, destination: string) {
  const stats = fs.statSync(source)
  if (stats.isDirectory()) {
    fs.mkdirSync(destination, { recursive: true })
    for (const child of fs.readdirSync(source)) {
      copyRecursiveSync(path.join(source, child), path.join(destination, child))
    }
    return
  }
  fs.copyFileSync(source, destination)
}

function createWorldSlug(name: string, worldsDir: string, stagingDir: string) {
  const base = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/(^-|-$)/g, '') || 'world'
  for (let attempt = 0; attempt < 10; attempt += 1) {
    const slug = `${base}-${Math.random().toString(36).slice(2, 8)}`
    if (!fs.existsSync(path.join(worldsDir, slug)) && !fs.existsSync(path.join(stagingDir, slug))) {
      return slug
    }
  }
  throw new Error('Could not allocate a unique world identifier')
}

function findPublishedJob(worldsDir: string, jobId: string) {
  if (!fs.existsSync(worldsDir)) return undefined
  for (const entry of fs.readdirSync(worldsDir, { withFileTypes: true })) {
    if (!entry.isDirectory() || entry.name.startsWith('.')) continue
    const projectPath = path.join(worldsDir, entry.name, 'project.json')
    if (!fs.existsSync(projectPath)) continue
    try {
      const project = JSON.parse(fs.readFileSync(projectPath, 'utf8')) as { reconstruction_job_id?: string }
      if (project.reconstruction_job_id === jobId) return entry.name
    } catch {
      // A malformed unrelated world should not block finalizing this one.
    }
  }
  return undefined
}

async function publishWorldDir(stagedDir: string, finalDir: string) {
  const retryDelaysMs = [100, 300, 700, 1500, 3000]
  const transient = (error: unknown) => (
    ['EPERM', 'EACCES', 'EBUSY', 'ENOTEMPTY'].includes((error as NodeJS.ErrnoException)?.code ?? '')
  )
  for (let attempt = 0; attempt <= retryDelaysMs.length; attempt += 1) {
    try {
      fs.renameSync(stagedDir, finalDir)
      return
    } catch (error) {
      if (!transient(error) || attempt === retryDelaysMs.length) break
      await new Promise((resolve) => setTimeout(resolve, retryDelaysMs[attempt]))
    }
  }
  fs.cpSync(stagedDir, finalDir, { recursive: true })
  fs.rmSync(stagedDir, { recursive: true, force: true })
}

async function buildCollider(worldOutDir: string) {
  const candidate = ['0-world-500k.ply', '0-world-full_res.ply']
    .map((name) => path.join(worldOutDir, name))
    .find((value) => fs.existsSync(value))
  if (!candidate) return undefined

  try {
    const formData = new FormData()
    formData.append('file', new Blob([new Uint8Array(fs.readFileSync(candidate))]), 'world.ply')
    // Nerfstudio's exported splat is already Y-up. SHARP uses the endpoint's
    // historical flip_y=true default, so make this pipeline explicit.
    formData.append('flip_y', 'false')
    formData.append('rotation_x', String(NERFSTUDIO_ROTATION_X))
    const response = await fetch(`${AI_BACKEND_URL}/api/splat-to-collider`, {
      method: 'POST',
      body: formData,
      signal: AbortSignal.timeout(180_000),
    })
    if (!response.ok) throw new Error((await response.text()).trim() || `HTTP ${response.status}`)
    const result = await response.json() as {
      collider_glb?: string
      ground_plane_offset?: number
    }
    if (result.collider_glb) {
      fs.writeFileSync(path.join(worldOutDir, '0-world.glb'), Buffer.from(result.collider_glb, 'base64'))
    }
    return typeof result.ground_plane_offset === 'number' ? result.ground_plane_offset : 0
  } catch (error) {
    console.warn('[Video reconstruction] Collision mesh unavailable:', error)
    return undefined
  }
}

export async function POST(request: NextRequest) {
  // Publishes a finished reconstruction into `public/worlds`, which means
  // writing hundreds of megabytes and claiming a slug. Same-origin only.
  if (!isAppRequest(request)) {
    return NextResponse.json({ error: 'Forbidden' }, { status: 403 })
  }

  let payload: { jobId?: string; name?: string }
  try {
    payload = await request.json() as { jobId?: string; name?: string }
  } catch {
    return NextResponse.json({ error: 'Invalid JSON request.' }, { status: 400 })
  }

  const jobId = payload.jobId?.trim() || ''
  const requestedName = payload.name?.trim() || ''
  if (!JOB_ID_PATTERN.test(jobId)) {
    return NextResponse.json({ error: 'Invalid reconstruction job ID.' }, { status: 400 })
  }

  try {
    const response = await fetch(`${AI_BACKEND_URL}/api/reconstruction-jobs/${jobId}`, {
      cache: 'no-store',
      signal: AbortSignal.timeout(10_000),
    })
    if (!response.ok) {
      return NextResponse.json({ error: 'Reconstruction job was not found.' }, { status: response.status })
    }
    const job = await response.json() as ReconstructionJob
    if (job.status !== 'complete') {
      return NextResponse.json({ error: `Reconstruction is ${job.status}, not complete.` }, { status: 409 })
    }

    const displayName = requestedName || job.name
    if (!displayName || displayName.length > 80) {
      return NextResponse.json({ error: 'World name must be 1-80 characters.' }, { status: 400 })
    }
    const outputPly = safeJobArtifact(jobId, job.output_ply)
    const sourceVideo = safeJobArtifact(jobId, job.source_path)
    const thumbnail = safeJobArtifact(jobId, job.thumbnail_path)
    if (!outputPly) {
      return NextResponse.json({ error: 'Reconstruction PLY is missing.' }, { status: 409 })
    }

    const worldsDir = path.join(process.cwd(), 'public', 'worlds')
    const alreadyPublished = findPublishedJob(worldsDir, jobId)
    if (alreadyPublished) return NextResponse.json({ success: true, slug: alreadyPublished })

    const stagingDir = path.join(worldsDir, '.staging')
    fs.mkdirSync(stagingDir, { recursive: true })
    const slug = createWorldSlug(displayName, worldsDir, stagingDir)
    const stagedWorldDir = path.join(stagingDir, slug)
    const finalWorldDir = path.join(worldsDir, slug)
    const sourceDir = path.join(stagedWorldDir, 'source')
    const worldOutDir = path.join(stagedWorldDir, 'output', 'world')
    fs.mkdirSync(sourceDir, { recursive: true })
    fs.mkdirSync(worldOutDir, { recursive: true })
    fs.mkdirSync(path.join(stagedWorldDir, 'output', 'sfx'), { recursive: true })

    try {
      if (sourceVideo) fs.copyFileSync(sourceVideo, path.join(sourceDir, `0-source${path.extname(sourceVideo)}`))
      if (thumbnail) fs.copyFileSync(thumbnail, path.join(worldOutDir, '0-world-thumbnail.jpg'))
      fs.copyFileSync(outputPly, path.join(worldOutDir, '0-world-full_res.ply'))

      const fullPly = fs.readFileSync(outputPly)
      for (const [label, count] of PLY_LOD_TARGETS) {
        const lod = createBinaryPlyLod(fullPly, count)
        if (lod && lod.length < fullPly.length) {
          fs.writeFileSync(path.join(worldOutDir, `0-world-${label}.ply`), lod)
        }
      }

      const rawGroundPlaneOffset = await buildCollider(worldOutDir) ?? 0
      const initialPosition = job.initial_view?.position
      const hasInitialPosition = Array.isArray(initialPosition)
        && initialPosition.length === 3
        && initialPosition.every(Number.isFinite)
      const initialYaw = job.initial_view?.yaw
      const initialPitch = job.initial_view?.pitch
      const now = new Date().toISOString()
      const requestedCameraHeight = job.camera_height
      const normalizedCameraHeight = hasInitialPosition
        ? initialPosition[1] + rawGroundPlaneOffset
        : 0
      const calibratedScale = typeof requestedCameraHeight === 'number'
        && Number.isFinite(requestedCameraHeight)
        && normalizedCameraHeight > 0.05
        ? requestedCameraHeight / normalizedCameraHeight
        : 1
      const metricScaleFactor = calibratedScale >= 0.1 && calibratedScale <= 20
        ? calibratedScale
        : 1
      const groundPlaneOffset = rawGroundPlaneOffset * metricScaleFactor
      fs.writeFileSync(path.join(stagedWorldDir, 'project.json'), JSON.stringify({
        slug,
        display_name: displayName,
        created_at: now,
        updated_at: now,
        reconstruction_job_id: jobId,
        notes: `Generated locally with COLMAP + Nerfstudio Splatfacto (${job.quality})`,
      }, null, 2))

      fs.writeFileSync(path.join(worldOutDir, '0-world.json'), JSON.stringify({
        world_id: slug,
        display_name: displayName,
        created_at: now,
        updated_at: now,
        assets: {
          caption: `${job.registered_frames ?? '?'} registered frames, ${job.gaussian_count ?? '?'} Gaussians`,
          splats: {
            spz_urls: { full_res: '' },
            semantics_metadata: {
              metric_scale_factor: metricScaleFactor,
              ground_plane_offset: groundPlaneOffset,
              flip_y: false,
              rotation_x: NERFSTUDIO_ROTATION_X,
              reconstruction_method: 'nerfstudio-splatfacto',
              camera_height_metres: job.camera_height ?? null,
            },
          },
        },
      }, null, 2))

      fs.writeFileSync(path.join(stagedWorldDir, 'scene.json'), JSON.stringify({
        version: 1,
        instances: [],
        sun: { intensity: 1, rotation: [0, 0, 0], environmentIntensity: 1 },
        metricScaleFactor,
        groundPlaneOffset,
        groundPlaneColliderEnabled: true,
        ...(hasInitialPosition ? {
          spawnPoint: [
            initialPosition[0] * metricScaleFactor,
            initialPosition[2] * metricScaleFactor,
          ],
        } : {}),
        ...(hasInitialPosition ? {
          spawnPosition: [
            initialPosition[0] * metricScaleFactor,
            initialPosition[1] * metricScaleFactor + groundPlaneOffset,
            initialPosition[2] * metricScaleFactor,
          ],
          defaultController: 'fly',
        } : {}),
        ...(typeof initialYaw === 'number' && Number.isFinite(initialYaw) ? { spawnYaw: initialYaw } : {}),
        ...(typeof initialPitch === 'number' && Number.isFinite(initialPitch) ? { spawnPitch: initialPitch } : {}),
      }, null, 2))

      // After the scale, not before: `metricScaleFactor` is calculated from the
      // collider's own floor height, so the grid could not have been measured
      // in metres during the collider pass. The spawn is what makes the
      // difference here -- a video world covers ground the walker cannot reach,
      // and flooding from the camera's actual start is the only way to say so.
      await buildWalkableMap(
        AI_BACKEND_URL,
        worldOutDir,
        {
          rotationX: NERFSTUDIO_ROTATION_X,
          metricScale: metricScaleFactor,
          spawn: hasInitialPosition
            ? [initialPosition[0] * metricScaleFactor, initialPosition[2] * metricScaleFactor]
            : undefined,
        },
      )

      const ambienceDir = path.join(process.cwd(), 'public', 'assets', 'ambience')
      if (fs.existsSync(ambienceDir)) {
        copyRecursiveSync(ambienceDir, path.join(stagedWorldDir, 'output', 'sfx'))
      }
      await publishWorldDir(stagedWorldDir, finalWorldDir)
      return NextResponse.json({ success: true, slug })
    } catch (error) {
      fs.rmSync(stagedWorldDir, { recursive: true, force: true })
      throw error
    }
  } catch (error) {
    console.error('[Video reconstruction] Finalization failed:', error)
    return NextResponse.json(
      { error: error instanceof Error ? error.message : 'Could not publish reconstructed world.' },
      { status: 500 },
    )
  }
}
