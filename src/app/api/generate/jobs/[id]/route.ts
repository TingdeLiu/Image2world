import { NextRequest, NextResponse } from 'next/server'
import { guardRoute } from '../../../../../utils/routeGuard'
import { cancelJob, isGenerationJobId, readJob } from '../../../../../utils/generationJobs'

export const dynamic = 'force-dynamic'

interface RouteContext {
  params: Promise<{ id: string }>
}

export async function GET(request: NextRequest, { params }: RouteContext) {
  const { denied } = await guardRoute(request)
  if (denied) return denied
  const { id } = await params
  if (!isGenerationJobId(id)) {
    return NextResponse.json({ error: 'Generation job not found.' }, { status: 404 })
  }
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
  const { denied } = await guardRoute(request)
  if (denied) return denied
  const { id } = await params
  if (!isGenerationJobId(id)) {
    return NextResponse.json({ error: 'Generation job not found.' }, { status: 404 })
  }
  const job = cancelJob(id)
  if (!job) return NextResponse.json({ error: 'Generation job not found.' }, { status: 404 })
  return NextResponse.json({ job })
}
