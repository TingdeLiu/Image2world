import type { WorldReconstruction } from '../types/world'

export function WorldQualityDetails({ reconstruction }: { reconstruction: WorldReconstruction }) {
  const { quality, scale } = reconstruction
  return (
    <details className="mx-2 min-w-0 whitespace-normal break-words rounded border border-white/10 bg-white/[0.03] text-[11px] text-white/60">
      <summary className="cursor-pointer rounded px-2 py-1.5 text-white/75 focus-visible:outline-2 focus-visible:outline-white/50">
        Reconstruction quality
      </summary>
      <div className="space-y-2 px-2 pb-2">
        {quality.status === 'measured' && quality.metrics ? (
          <>
            <p>Compared with {quality.evaluation_views} views excluded from appearance training.</p>
            <dl className="grid grid-cols-3 gap-1 text-center">
              <div title="Image fidelity; higher is better"><dt>PSNR ↑</dt><dd className="text-white/85">{quality.metrics.psnr.toFixed(1)} dB</dd></div>
              <div title="Structural similarity; higher is better"><dt>SSIM ↑</dt><dd className="text-white/85">{quality.metrics.ssim.toFixed(3)}</dd></div>
              <div title="Perceptual difference; lower is better"><dt>LPIPS ↓</dt><dd className="text-white/85">{quality.metrics.lpips.toFixed(3)}</dd></div>
            </dl>
            {reconstruction.comparison_urls?.map((url, index) => (
              <a key={url} href={url} target="_blank" rel="noreferrer" className="block underline decoration-white/25 hover:text-white">
                Compare observation / render {index + 1}
              </a>
            ))}
            <p className="text-white/45">Appearance only. Geometry and navigation need separate checks.</p>
          </>
        ) : <p>Not evaluated. {quality.reason}</p>}
        <p>
          Initial scale: {scale.kind === 'camera-height-estimate'
            ? `estimated from ${scale.camera_height_metres?.toFixed(2)} m camera height`
            : 'arbitrary units; no metric calibration'}.
        </p>
        <div className="flex flex-wrap gap-x-3 gap-y-1">
          {reconstruction.report_url && <a href={reconstruction.report_url} target="_blank" rel="noreferrer" className="underline decoration-white/25 hover:text-white">Quality report</a>}
          {reconstruction.cameras_url && <a href={reconstruction.cameras_url} target="_blank" rel="noreferrer" className="underline decoration-white/25 hover:text-white">Camera poses &amp; scale</a>}
        </div>
      </div>
    </details>
  )
}
