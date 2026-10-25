import type { WorldAppearanceQuality, WorldReconstruction } from '../types/world'

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : undefined
}

function finite(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

export function summarizeAppearanceQuality(value: unknown): WorldAppearanceQuality {
  const report = record(value)
  const metrics = record(report?.metrics)
  if (report?.status === 'measured' && report.protocol === 'held-out-rgb-v1'
    && finite(report.train_views) && Number.isInteger(report.train_views) && report.train_views > 0
    && finite(report.evaluation_views) && Number.isInteger(report.evaluation_views) && report.evaluation_views > 0
    && finite(metrics?.psnr) && finite(metrics?.ssim) && finite(metrics?.lpips)) {
    return {
      status: 'measured', train_views: report.train_views, evaluation_views: report.evaluation_views,
      metrics: { psnr: metrics.psnr, ssim: metrics.ssim, lpips: metrics.lpips },
    }
  }
  return {
    status: 'unavailable',
    reason: report?.status === 'unavailable' && typeof report.reason === 'string'
      ? report.reason.slice(0, 300) : 'No verified held-out evaluation is available.',
  }
}

export function reconstructionScale(cameraHeight: unknown, reconstructedHeight: number) {
  const candidate = finite(cameraHeight) && cameraHeight >= 0.5 && cameraHeight <= 3
    && finite(reconstructedHeight) && reconstructedHeight > 0.05
    ? cameraHeight / reconstructedHeight : Number.NaN
  const calibrated = finite(candidate) && candidate >= 0.1 && candidate <= 20
  const factor = calibrated ? candidate : 1
  const provenance: WorldReconstruction['scale'] = calibrated
    ? { kind: 'camera-height-estimate', factor, camera_height_metres: cameraHeight as number }
    : { kind: 'arbitrary', factor }
  return { factor, provenance }
}

/** Generation-time transform. Scene-editor changes are stored in scene.json. */
export function splatToInitialViewer(scale: number, groundOffset: number, rotationX: number): number[][] {
  const c = Math.cos(rotationX), s = Math.sin(rotationX)
  return [[scale, 0, 0, 0], [0, c * scale, -s * scale, groundOffset],
    [0, s * scale, c * scale, 0], [0, 0, 0, 1]]
}

/** Read disk metadata without trusting URLs or a malformed success label. */
export function normalizeWorldReconstruction(value: unknown): WorldReconstruction | undefined {
  const input = record(value)
  if (!input) return undefined
  const quality = record(input.quality)
  const scale = record(input.scale)
  const validScale = finite(scale?.factor) && scale.factor > 0
  const estimated = validScale && scale.kind === 'camera-height-estimate'
    && finite(scale.camera_height_metres) && scale.camera_height_metres >= 0.5 && scale.camera_height_metres <= 3
  const transform = input.splat_to_initial_viewer
  return {
    protocol: 'held-out-rgb-v1',
    quality: summarizeAppearanceQuality({ ...quality, protocol: input.protocol }),
    scale: estimated
      ? { kind: 'camera-height-estimate', factor: scale.factor as number, camera_height_metres: scale.camera_height_metres as number }
      : { kind: 'arbitrary', factor: validScale ? scale.factor as number : 1 },
    ...(Array.isArray(transform) && transform.length === 4
      && transform.every((row) => Array.isArray(row) && row.length === 4 && row.every(finite))
      ? { splat_to_initial_viewer: transform as number[][] } : {}),
  }
}
