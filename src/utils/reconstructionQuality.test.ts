import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { normalizeWorldReconstruction, reconstructionScale, splatToInitialViewer, summarizeAppearanceQuality } from './reconstructionQuality.ts'

const report = {
  protocol: 'held-out-rgb-v1', status: 'measured', train_views: 90, evaluation_views: 10,
  metrics: { psnr: 25.4, ssim: 0.82, lpips: 0.14 },
}

describe('held-out reconstruction quality', () => {
  it('does not present missing, empty, or malformed evaluations as measured', () => {
    for (const input of [undefined, {}, { ...report, evaluation_views: 0 },
      { ...report, train_views: 0 }, { ...report, protocol: 'training-rgb' },
      { ...report, metrics: { ...report.metrics, psnr: Number.NaN } }]) {
      assert.equal(summarizeAppearanceQuality(input).status, 'unavailable')
    }
    assert.equal(summarizeAppearanceQuality(report).status, 'measured')
  })

  it('never adopts arbitrary download links from a world manifest', () => {
    const result = normalizeWorldReconstruction({
      protocol: report.protocol, quality: report, scale: { kind: 'arbitrary', factor: 1 },
      report_url: 'https://untrusted.example/report', cameras_url: '../../private',
    })
    assert.equal(result?.quality.status, 'measured')
    assert.equal(result?.report_url, undefined)
    assert.equal(result?.cameras_url, undefined)
  })
})

describe('reconstruction scale provenance', () => {
  it('keeps uncalibrated or rejected scales explicitly arbitrary', () => {
    for (const [height, reconstructed] of [[undefined, 1], [1.6, 0], [1.6, 0.06], [Number.NaN, 1]]) {
      assert.deepEqual(reconstructionScale(height, reconstructed as number), {
        factor: 1, provenance: { kind: 'arbitrary', factor: 1 },
      })
    }
  })

  it('labels even a unit scale as estimated when camera height actually calibrated it', () => {
    const calibration = reconstructionScale(1.6, 1.6)
    assert.equal(calibration.factor, 1)
    assert.equal(calibration.provenance.kind, 'camera-height-estimate')
  })

  it('maps recorded cameras to the same ground-relative viewer frame as the splat', () => {
    // A camera at z=0.9 above a z=0.1 floor should end up 1.6 metres above
    // the calibrated floor: scale=2, ground translation=-0.2.
    const calibration = reconstructionScale(1.6, 0.8)
    const matrix = splatToInitialViewer(calibration.factor, -0.2, -Math.PI / 2)
    const camera = [1, 2, 0.9, 1]
    const mapped = matrix.map((row) => row.reduce((sum, v, i) => sum + v * camera[i], 0))
    assert.ok(Math.abs(mapped[1] - 1.6) < 1e-12)
    assert.ok(Math.abs(mapped[2] + 4) < 1e-12)
    assert.equal(mapped[0], 2)
  })
})
