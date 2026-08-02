import { NextResponse } from 'next/server'
import { openClaudeTerminal } from '../../../utils/worldsScanner'
import { LOCAL_TOOLS_ENABLED } from '../../../utils/localTools'

export const dynamic = 'force-dynamic'

export async function GET() {
  // Spawning a terminal on the host is a developer convenience locally and a
  // remote-code-execution surface once deployed. 404 rather than 403: a
  // disabled endpoint should not advertise that it exists.
  if (!LOCAL_TOOLS_ENABLED) {
    return new NextResponse('Not found', { status: 404 })
  }
  if (!openClaudeTerminal()) {
    return new NextResponse('Opening Claude terminal is only supported on macOS.', { status: 501 })
  }
  return new NextResponse(null, { status: 204 })
}
