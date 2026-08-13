import { NextRequest, NextResponse } from 'next/server'
import { openClaudeTerminal } from '../../../utils/worldsScanner'
import { LOCAL_TOOLS_ENABLED } from '../../../utils/localTools'
import { isAppRequest } from '../../../utils/sameOrigin'

export const dynamic = 'force-dynamic'

export async function GET(request: NextRequest) {
  // Spawning a terminal on the host is a developer convenience locally and a
  // remote-code-execution surface once deployed. 404 rather than 403: a
  // disabled endpoint should not advertise that it exists.
  //
  // The deployment gate is not enough on its own. In development the tools are
  // on by design, and any page in another tab can reach a localhost route --
  // so the caller must also prove it is this app.
  if (!LOCAL_TOOLS_ENABLED || !isAppRequest(request)) {
    return new NextResponse('Not found', { status: 404 })
  }
  if (!openClaudeTerminal()) {
    return new NextResponse('Opening Claude terminal is only supported on macOS.', { status: 501 })
  }
  return new NextResponse(null, { status: 204 })
}
