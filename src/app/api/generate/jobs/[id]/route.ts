import { NextRequest, NextResponse } from 'next/server'
import { isAppRequest } from '../../../../../utils/sameOrigin'
import { cancelJob, readJob } from '../../../../../utils/generationJobs'

export const dynamic = 'force-dynamic'

interface RouteContext {
  params: Promise<{ id: string }>
}

export async function GET(request: NextRequest, { params }: RouteContext) {
  if (!isAppRequest(request)) {
    return NextResponse.json({ error: 'Forbidden' }, { status: 403 })
  }
  const { id } = await params
  const job = readJob(id)
  if (!job) return NextResponse.json({ error: 'Generation job not found.' }, { status: 404 })
  return NextResponse.json({ job })
}

/**
 * Stop a generation. Idempotent: a job that already finished comes back as it
 * is rather than as an error, because "cancel" from a browser that has not seen
 * the last poll yet is a race, not a mistake.
 */
export async function DELETE(request: NextRequest, { params }: RouteContext) {
  if (!isAppRequest(request)) {
    return NextResponse.json({ error: 'Forbidden' }, { status: 403 })
  }
  const { id } = await params
  const job = cancelJob(id)
  if (!job) return NextResponse.json({ error: 'Generation job not found.' }, { status: 404 })
  return NextResponse.json({ job })
}
