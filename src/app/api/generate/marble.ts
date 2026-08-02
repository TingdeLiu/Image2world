import fs from 'fs'
import path from 'path'

/**
 * World Labs Marble backend.
 *
 * Marble is a generative world model: it invents the parts of the room the
 * camera never saw. That is the one thing single-view reconstruction cannot do,
 * and it is why a Marble world is sealed on all sides while a SHARP world is
 * open behind the viewer.
 *
 * Callers supply the API key -- it belongs to the user, is never persisted
 * server-side, and must stay out of logs.
 */

const MARBLE_API = 'https://api.worldlabs.ai/marble/v1'
const POLL_INTERVAL_MS = 10_000
const GENERATION_TIMEOUT_MS = 15 * 60_000

/** Splat detail levels Marble returns, in the order we prefer to fetch them. */
const SPZ_KEYS = ['full_res', '500k', '150k', '100k'] as const

export interface MarbleSemantics {
  metricScaleFactor: number
  groundPlaneOffset: number
}

export class MarbleError extends Error {
  constructor(message: string, readonly retryable = true) {
    super(message)
    this.name = 'MarbleError'
  }
}

interface MarbleRequestInit extends RequestInit {
  apiKey: string
}

async function marbleFetch(pathname: string, { apiKey, ...init }: MarbleRequestInit, signal: AbortSignal) {
  const response = await fetch(`${MARBLE_API}${pathname}`, {
    ...init,
    signal,
    headers: { ...(init.headers ?? {}), 'WLT-Api-Key': apiKey },
  })
  if (!response.ok) {
    const detail = (await response.text()).trim().slice(0, 300)
    if (response.status === 401 || response.status === 403) {
      throw new MarbleError('Marble rejected the API key. Check it on platform.worldlabs.ai.', false)
    }
    if (response.status === 402) {
      throw new MarbleError('Marble reports insufficient credits for this generation.', false)
    }
    throw new MarbleError(`Marble request failed (HTTP ${response.status})${detail ? `: ${detail}` : ''}`)
  }
  return response
}

/** Confirm a key works and report what is left. Cheap, and never bills. */
export async function fetchMarbleCredits(apiKey: string, signal: AbortSignal) {
  const res = await marbleFetch('/credits', { apiKey, cache: 'no-store' }, signal)
  const data = await res.json() as { remaining_credits?: number }
  return typeof data.remaining_credits === 'number' ? data.remaining_credits : undefined
}

async function uploadSourceImage(apiKey: string, imageBuffer: Buffer, signal: AbortSignal) {
  const prepared = await marbleFetch('/media-assets:prepare_upload', {
    apiKey,
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ file_name: 'source', kind: 'image', extension: 'png' }),
  }, signal)

  const { media_asset: mediaAsset, upload_info: uploadInfo } = await prepared.json() as {
    media_asset: { media_asset_id: string }
    upload_info: { upload_url: string; required_headers?: Record<string, string> }
  }

  // The signed URL goes straight to storage, so it takes no API key.
  const put = await fetch(uploadInfo.upload_url, {
    method: 'PUT',
    body: new Uint8Array(imageBuffer),
    headers: uploadInfo.required_headers ?? {},
    signal,
  })
  if (!put.ok) throw new MarbleError(`Uploading the source image failed (HTTP ${put.status})`)

  return mediaAsset.media_asset_id
}

/**
 * Nudge Marble to close the room off.
 *
 * Its own examples are descriptive rather than technical, and it already
 * extrapolates unseen areas, so describing what stands behind the camera works
 * far better than forbidding gaps.
 */
const ENCLOSURE_HINT =
  'A complete, enclosed interior. Beyond the photographed view, the space continues behind the ' +
  'viewpoint with its own wall, and the floor and ceiling extend unbroken across the whole room, ' +
  'so someone standing inside can turn a full circle and always face an enclosing surface.'

async function startGeneration(
  apiKey: string,
  mediaAssetId: string,
  displayName: string,
  signal: AbortSignal,
) {
  const res = await marbleFetch('/worlds:generate', {
    apiKey,
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      display_name: displayName.slice(0, 64),
      model: 'marble-1.1',
      world_prompt: {
        type: 'image',
        image_prompt: { source: 'media_asset', media_asset_id: mediaAssetId },
        text_prompt: ENCLOSURE_HINT,
      },
    }),
  }, signal)
  const { operation_id: operationId } = await res.json() as { operation_id?: string }
  if (!operationId) throw new MarbleError('Marble did not return an operation id.')
  return operationId
}

interface MarbleWorldAssets {
  mesh?: { collider_mesh_url?: string | null }
  imagery?: { pano_url?: string | null }
  splats?: {
    spz_urls?: Record<string, string | undefined>
    semantics_metadata?: { metric_scale_factor?: number; ground_plane_offset?: number }
  }
}

async function awaitCompletion(
  apiKey: string,
  operationId: string,
  onProgress: (elapsedMs: number) => void,
  signal: AbortSignal,
) {
  const startedAt = Date.now()

  for (;;) {
    signal.throwIfAborted()
    if (Date.now() - startedAt > GENERATION_TIMEOUT_MS) {
      throw new MarbleError(`Marble did not finish within ${GENERATION_TIMEOUT_MS / 60_000} minutes.`)
    }

    const res = await marbleFetch(`/operations/${operationId}`, { apiKey, cache: 'no-store' }, signal)
    const op = await res.json() as {
      done?: boolean
      error?: unknown
      response?: { world_id?: string; assets?: MarbleWorldAssets }
    }

    if (op.done) {
      if (op.error) {
        const detail = typeof op.error === 'string' ? op.error : JSON.stringify(op.error).slice(0, 300)
        throw new MarbleError(`Marble generation failed: ${detail}`)
      }
      if (!op.response?.assets) throw new MarbleError('Marble finished but returned no assets.')
      return op.response
    }

    onProgress(Date.now() - startedAt)
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS))
  }
}

async function downloadTo(url: string, destination: string, signal: AbortSignal) {
  const res = await fetch(url, { signal })
  if (!res.ok) throw new MarbleError(`Downloading ${path.basename(destination)} failed (HTTP ${res.status})`)
  fs.writeFileSync(destination, Buffer.from(await res.arrayBuffer()))
}

export interface MarbleGenerationInput {
  apiKey: string
  imageBuffer: Buffer
  displayName: string
  slug: string
  worldOutDir: string
  report: (progress: number, message: string, detail?: string) => void
  signal: AbortSignal
}

/**
 * Run a full Marble generation and lay the assets out the way the scanner
 * expects. Returns the scale/offset the scene descriptor needs.
 */
export async function generateWorldWithMarble({
  apiKey,
  imageBuffer,
  displayName,
  slug,
  worldOutDir,
  report,
  signal,
}: MarbleGenerationInput): Promise<MarbleSemantics> {
  report(10, 'Uploading source image', 'Sending the photo to World Labs Marble')
  const mediaAssetId = await uploadSourceImage(apiKey, imageBuffer, signal)

  report(16, 'Generating the world', 'Marble is building a complete 3D space — this takes several minutes')
  const operationId = await startGeneration(apiKey, mediaAssetId, displayName, signal)

  const world = await awaitCompletion(apiKey, operationId, (elapsedMs) => {
    const seconds = Math.round(elapsedMs / 1000)
    // A typical run lands around eight minutes; ease towards 80% over that span
    // instead of sitting at a single number for the whole wait.
    const progress = Math.min(80, 16 + (seconds / 480) * 64)
    report(
      Math.round(progress),
      'Generating the world',
      `Marble is imagining the parts the photo could not see · ${seconds}s elapsed`,
    )
  }, signal)

  const assets = world.assets ?? {}
  const spzUrls = assets.splats?.spz_urls ?? {}
  const available = SPZ_KEYS.filter((key) => spzUrls[key])
  if (!available.length) throw new MarbleError('Marble returned no splat assets.')

  report(84, 'Downloading world assets', `${available.length} detail levels, collision mesh, and panorama`)
  for (const key of available) {
    await downloadTo(spzUrls[key]!, path.join(worldOutDir, `0-world-${key}.spz`), signal)
  }

  const colliderUrl = assets.mesh?.collider_mesh_url
  if (colliderUrl) {
    await downloadTo(colliderUrl, path.join(worldOutDir, '0-world.glb'), signal)
  }
  const panoUrl = assets.imagery?.pano_url
  if (panoUrl) {
    await downloadTo(panoUrl, path.join(worldOutDir, '0-world-pano.png'), signal)
  }

  // Marble emits normalised coordinates -- metric_scale_factor is what turns
  // them into metres, and is emphatically not 1.
  const semantics = assets.splats?.semantics_metadata ?? {}
  const metricScaleFactor = typeof semantics.metric_scale_factor === 'number' ? semantics.metric_scale_factor : 1
  const groundPlaneOffset = typeof semantics.ground_plane_offset === 'number' ? semantics.ground_plane_offset : 0

  const worldJson = {
    world_id: slug,
    display_name: displayName,
    assets: {
      splats: {
        spz_urls: Object.fromEntries(available.map((key) => [key, ''])),
        semantics_metadata: {
          metric_scale_factor: metricScaleFactor,
          ground_plane_offset: groundPlaneOffset,
          flip_y: true,
        },
      },
    },
  }
  fs.writeFileSync(path.join(worldOutDir, '0-world.json'), JSON.stringify(worldJson, null, 2))

  return { metricScaleFactor, groundPlaneOffset }
}
