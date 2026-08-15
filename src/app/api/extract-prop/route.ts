import { NextRequest, NextResponse } from 'next/server'
import fs from 'fs'
import path from 'path'
import { sceneProjectPath } from '../../../utils/worldsScanner'
import { isAppRequest } from '../../../utils/sameOrigin'

export const dynamic = 'force-dynamic'
// Segmentation plus voxelisation of a full-resolution cloud runs for tens of
// seconds, and the first call also pays for loading SAM 2 onto the GPU.
export const maxDuration = 300

const AI_BACKEND_URL = (process.env.IMAGEWORLD_BACKEND_URL || 'http://localhost:8000').replace(/\/+$/, '')
const BACKEND_TIMEOUT_MS = 180_000
const PROPS_GLB = '0-world-props.glb'
const PROPS_JSON = '0-world-props.json'

interface PropEntry {
  id: string
  label?: string
  mass: number
  centre: [number, number, number]
}

function badRequest(message: string, status = 400) {
  return NextResponse.json({ error: message }, { status })
}

/** The world's `output/world` directory, or null if the slug escapes it. */
function worldOutputDir(slug: string | null): string | null {
  if (!slug) return null
  const sceneFile = sceneProjectPath(slug)
  if (!sceneFile) return null
  const dir = path.join(path.dirname(sceneFile), 'output', 'world')
  return fs.existsSync(dir) ? dir : null
}

function readPropEntries(outDir: string): PropEntry[] {
  const file = path.join(outDir, PROPS_JSON)
  if (!fs.existsSync(file)) return []
  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf-8')) as { props?: unknown }
    return Array.isArray(parsed.props) ? (parsed.props as PropEntry[]) : []
  } catch {
    return []
  }
}

/** `prop-N` past the highest existing N, so ids stay stable as props come and go. */
function nextPropId(existing: PropEntry[]): string {
  const highest = existing.reduce((max, entry) => {
    const match = /^prop-(\d+)$/.exec(entry.id ?? '')
    return match ? Math.max(max, Number(match[1])) : max
  }, -1)
  return `prop-${highest + 1}`
}

function findFile(dir: string, pattern: RegExp): string | undefined {
  return fs.readdirSync(dir).find((name) => pattern.test(name))
}

const positiveNumber = (value: unknown) =>
  typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : undefined

function readJson(file: string): Record<string, unknown> | undefined {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf-8')) as Record<string, unknown>
  } catch {
    return undefined
  }
}

/**
 * Reconstruction units per metre, read in the viewer's own order of preference.
 *
 * The backend measures a prop's mass from its volume, and volume cubes the
 * error — on a world reconstructed at 3.66 units per metre an ungoverned chair
 * comes back at 49x its weight, which reads as "the physics is broken" rather
 * than "a number was in the wrong unit". scene.json wins over the manifest
 * because that is what the viewer renders with.
 */
function readMetricScale(slug: string, outDir: string): number {
  const sceneFile = sceneProjectPath(slug)
  if (sceneFile && fs.existsSync(sceneFile)) {
    const found = positiveNumber(readJson(sceneFile)?.metricScaleFactor)
    if (found !== undefined) return found
  }

  const manifestName = findFile(outDir, /-world\.json$/)
  if (manifestName) {
    const manifest = readJson(path.join(outDir, manifestName)) as
      | { assets?: { splats?: { semantics_metadata?: Record<string, unknown> } } }
      | undefined
    const found = positiveNumber(manifest?.assets?.splats?.semantics_metadata?.metric_scale_factor)
    if (found !== undefined) return found
  }

  return 1
}

/**
 * Carve one movable prop out of a world.
 *
 * The browser only knows where the user clicked; everything the extractor needs
 * — the gaussians, the source photo, the props file so far — lives on disk
 * here. This route gathers them, hands them to the Python service, and writes
 * back what comes out. It never parses a GLB: the backend returns the whole
 * merged props file precisely so this side only moves bytes.
 */
export async function POST(request: NextRequest) {
  // Segmentation plus voxelisation occupies the GPU for tens of seconds and
  // rewrites the world's props on disk. Neither should be reachable from a page
  // the user merely has open somewhere else.
  if (!isAppRequest(request)) return badRequest('Forbidden', 403)

  const { searchParams } = new URL(request.url)
  const outDir = worldOutputDir(searchParams.get('slug'))
  if (!outDir) return badRequest('Unknown world.', 404)

  let body: { point?: unknown; box?: unknown }
  try {
    body = await request.json()
  } catch {
    return badRequest('Expected a JSON body with `point` or `box`.')
  }

  const isVec3 = (value: unknown): value is number[] =>
    Array.isArray(value) && value.length === 3 && value.every((n) => typeof n === 'number' && Number.isFinite(n))
  const hasPoint = isVec3(body.point)
  const hasBox = Array.isArray(body.box) && body.box.length === 2 && body.box.every(isVec3)
  if (hasPoint === hasBox) {
    return badRequest('Give exactly one of `point` (a splat-space [x,y,z]) or `box` ([[x,y,z],[x,y,z]]).')
  }

  // Extraction reads individual gaussians, so it wants the full cloud rather
  // than a decimated LOD — a 100k LOD has nothing left to voxelise per object.
  const plyName = findFile(outDir, /-world-full_res\.ply$/)
  if (!plyName) return badRequest('This world has no full-resolution point cloud to cut from.', 422)

  const form = new FormData()
  form.append('file', new Blob([new Uint8Array(fs.readFileSync(path.join(outDir, plyName)))]), 'world.ply')

  form.append('metric_scale', String(readMetricScale(searchParams.get('slug') ?? '', outDir)))

  const existingProps = readPropEntries(outDir)
  const propId = nextPropId(existingProps)
  form.append('prop_id', propId)

  const glbPath = path.join(outDir, PROPS_GLB)
  if (fs.existsSync(glbPath)) {
    form.append('existing', new Blob([new Uint8Array(fs.readFileSync(glbPath))]), PROPS_GLB)
  }

  if (hasPoint) {
    const sourceDir = path.join(path.dirname(path.dirname(outDir)), 'source')
    const sourceName = fs.existsSync(sourceDir) ? findFile(sourceDir, /\.(png|jpe?g|webp)$/i) : undefined
    if (!sourceName) return badRequest('This world has no source image, so a click cannot be segmented.', 422)
    form.append('image', new Blob([new Uint8Array(fs.readFileSync(path.join(sourceDir, sourceName)))]), sourceName)
    form.append('point', JSON.stringify(body.point))
  } else {
    form.append('box', JSON.stringify(body.box))
  }

  let response: Response
  try {
    response = await fetch(`${AI_BACKEND_URL}/api/extract-prop`, {
      method: 'POST',
      body: form,
      signal: AbortSignal.timeout(BACKEND_TIMEOUT_MS),
    })
  } catch {
    return badRequest(`Could not reach the AI backend at ${AI_BACKEND_URL}. Start backend/server.py.`, 503)
  }

  if (!response.ok) {
    const detail = (await response.text()).trim()
    // 422 is the backend saying the selection was unusable, which is the
    // user's problem to fix by clicking somewhere else.
    return NextResponse.json(
      { error: detail || `Prop extraction failed with HTTP ${response.status}.` },
      { status: response.status === 422 ? 422 : 502 },
    )
  }

  const result = await response.json() as {
    props_glb: string
    prop_id: string
    mass: number
    centre: [number, number, number]
    source?: string
    gaussian_count?: number
  }

  const entry: PropEntry = {
    id: result.prop_id ?? propId,
    label: result.source?.startsWith('SAM') ? 'segmented' : 'boxed',
    mass: result.mass,
    centre: result.centre,
  }
  // Re-cutting an existing prop replaces it rather than adding a twin.
  const props = [...existingProps.filter((item) => item.id !== entry.id), entry]

  try {
    fs.writeFileSync(glbPath, Buffer.from(result.props_glb, 'base64'))
    fs.writeFileSync(path.join(outDir, PROPS_JSON), `${JSON.stringify({ version: 1, props }, null, 2)}\n`)
  } catch (error) {
    console.error('Could not write props for this world:', error)
    return badRequest('Extracted the prop but could not save it.', 500)
  }

  console.log(`[Props] ${entry.id} (${entry.mass} kg) from ${result.source ?? 'selection'}; ${props.length} total`)
  return NextResponse.json({ propId: entry.id, mass: entry.mass, props })
}
