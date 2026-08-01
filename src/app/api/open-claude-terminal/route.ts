import { NextResponse } from 'next/server'
import { openClaudeTerminal } from '../../../utils/worldsScanner'

export const dynamic = 'force-dynamic'

export async function GET() {
  if (!openClaudeTerminal()) {
    return new NextResponse('Opening Claude terminal is only supported on macOS.', { status: 501 })
  }
  return new NextResponse(null, { status: 204 })
}
