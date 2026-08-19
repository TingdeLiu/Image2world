import { NextRequest, NextResponse } from 'next/server'
import { isAppRequest } from '../../../../utils/sameOrigin'
import { activeJob, listJobs } from '../../../../utils/generationJobs'

export const dynamic = 'force-dynamic'

/**
 * The generations this server knows about, newest first, plus whichever is
 * still in flight.
 *
 * `active` is what a reloaded page reads: a generation now outlives the request
 * that started it, so the browser needs a way to find its way back to one
 * without having remembered an id across the refresh.
 */
export async function GET(request: NextRequest) {
  if (!isAppRequest(request)) {
    return NextResponse.json({ error: 'Forbidden' }, { status: 403 })
  }
  return NextResponse.json({ jobs: listJobs(), active: activeJob() ?? null })
}
