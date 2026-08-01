import { NextResponse } from 'next/server'
import { readWorlds } from '../../../utils/worldsScanner'

export const dynamic = 'force-dynamic'

export async function GET() {
  try {
    const worlds = readWorlds()
    return NextResponse.json(worlds, {
      headers: {
        'Cache-Control': 'no-store, max-age=0, must-revalidate',
      },
    })
  } catch (error) {
    console.error('Failed to read worlds:', error)
    return new NextResponse('Failed to read worlds', { status: 500 })
  }
}
