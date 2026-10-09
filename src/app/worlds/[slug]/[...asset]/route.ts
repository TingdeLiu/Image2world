import fs from 'node:fs'
import path from 'node:path'
import { Readable } from 'node:stream'
import { NextRequest, NextResponse } from 'next/server'
import { worldDirPath } from '../../../../utils/worldsScanner'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

// `next start` indexes public/ at startup. Files generated afterwards need a
// runtime fallback; middleware still protects the same /worlds/* URLs.
const CONTENT_TYPES: Record<string, string> = {
  '.json': 'application/json',
  '.glb': 'model/gltf-binary',
  '.gltf': 'model/gltf+json',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.mp3': 'audio/mpeg',
  '.wav': 'audio/wav',
  '.ogg': 'audio/ogg',
  '.mp4': 'video/mp4',
  '.webm': 'video/webm',
}

function isInside(root: string, file: string) {
  const relative = path.relative(root, file)
  return relative !== '' && relative !== '..'
    && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative)
}

interface RouteContext {
  params: Promise<{ slug: string; asset: string[] }>
}

async function serveAsset(request: NextRequest, { params }: RouteContext) {
  const { slug, asset } = await params
  const worldDir = worldDirPath(slug)
  if (!worldDir || !asset.length || asset.some((segment) =>
    segment === '.' || segment === '..' || /[/\\\0]/.test(segment),
  )) return new NextResponse('Not found', { status: 404 })

  let file: string
  let stat: fs.Stats
  try {
    const root = fs.realpathSync(path.join(process.cwd(), 'public', 'worlds'))
    const directory = fs.realpathSync(worldDir)
    file = fs.realpathSync(path.join(worldDir, ...asset))
    if (!isInside(root, directory) || !isInside(directory, file)) {
      return new NextResponse('Not found', { status: 404 })
    }
    stat = fs.statSync(file)
    if (!stat.isFile()) return new NextResponse('Not found', { status: 404 })
  } catch {
    return new NextResponse('Not found', { status: 404 })
  }

  const etag = `W/"${stat.size.toString(16)}-${stat.mtimeMs.toString(16)}"`
  const headers = new Headers({
    'Content-Type': CONTENT_TYPES[path.extname(file).toLowerCase()] || 'application/octet-stream',
    'Content-Length': String(stat.size),
    'Accept-Ranges': 'bytes',
    'Cache-Control': 'private, max-age=0, must-revalidate',
    'Last-Modified': stat.mtime.toUTCString(),
    'ETag': etag,
    'X-Content-Type-Options': 'nosniff',
  })
  if (request.headers.get('if-none-match') === etag) {
    headers.delete('Content-Length')
    return new NextResponse(null, { status: 304, headers })
  }
  if (request.method === 'HEAD' || stat.size === 0) {
    return new NextResponse(null, { headers })
  }

  let start = 0
  let end = stat.size - 1
  const range = request.headers.get('range')
  if (range) {
    const match = /^bytes=(\d*)-(\d*)$/.exec(range)
    if (match && (match[1] || match[2])) {
      start = match[1] ? Number(match[1]) : Math.max(0, stat.size - Number(match[2]))
      end = match[1] && match[2] ? Math.min(Number(match[2]), end) : end
    }
    if (!match || (!match[1] && !match[2]) || !Number.isSafeInteger(start)
      || !Number.isSafeInteger(end) || start > end || start >= stat.size
      || (!match[1] && Number(match[2]) === 0)) {
      headers.set('Content-Range', `bytes */${stat.size}`)
      headers.set('Content-Length', '0')
      return new NextResponse(null, { status: 416, headers })
    }
    headers.set('Content-Range', `bytes ${start}-${end}/${stat.size}`)
    headers.set('Content-Length', String(end - start + 1))
  }

  // Stream large splats instead of buffering hundreds of megabytes per request.
  const stream = Readable.toWeb(fs.createReadStream(file, { start, end })) as ReadableStream<Uint8Array>
  return new NextResponse(stream, { status: range ? 206 : 200, headers })
}

export const GET = serveAsset
export const HEAD = serveAsset
