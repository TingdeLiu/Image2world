import { NextRequest, NextResponse } from 'next/server'

export const dynamic = 'force-dynamic'
export const maxDuration = 60

const AI_BACKEND_URL = (process.env.IMAGEWORLD_BACKEND_URL || 'http://localhost:8000').replace(/\/+$/, '')

// Thin proxy so the browser only ever talks to Next routes; forwards the click
// (image + x/y) to the Python SAM 2 point-prompt endpoint and relays the mask.
export async function POST(request: NextRequest) {
  try {
    const formData = await request.formData()
    const res = await fetch(`${AI_BACKEND_URL}/api/segment-point`, {
      method: 'POST',
      body: formData,
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(45_000)]),
    })
    const buf = Buffer.from(await res.arrayBuffer())
    return new NextResponse(buf, {
      status: res.status,
      headers: { 'Content-Type': res.headers.get('content-type') || 'application/json' },
    })
  } catch (error: unknown) {
    if (request.signal.aborted) {
      return NextResponse.json(
        { error: { code: 'request_canceled', message: 'Point selection canceled.' } },
        { status: 499 },
      )
    }
    if (error instanceof DOMException && error.name === 'TimeoutError') {
      return NextResponse.json(
        { error: { code: 'backend_timeout', message: 'Point selection timed out after 45 seconds.' } },
        { status: 504 },
      )
    }
    const message = error instanceof Error ? error.message : 'segment-point failed'
    return NextResponse.json(
      { error: { code: 'segment_failed', message } },
      { status: 500 },
    )
  }
}
