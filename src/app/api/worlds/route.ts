import fs from 'fs'
import path from 'path'
import { NextRequest, NextResponse } from 'next/server'
import { readWorlds, worldDirPath } from '../../../utils/worldsScanner'
import { LOCAL_TOOLS_ENABLED } from '../../../utils/localTools'
import { isAppRequest } from '../../../utils/sameOrigin'

export const dynamic = 'force-dynamic'
// Removing a world means removing hundreds of megabytes, which is not instant.
export const maxDuration = 120

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

/**
 * Delete a world and everything under it.
 *
 * Gated the same way "open world folder" is, and for the same reason: this acts
 * on the disk of whichever machine runs the server. A same-origin check is not
 * enough on its own here -- a visitor to a deployed instance is same-origin, and
 * nothing about this is recoverable for them or for the operator.
 *
 * The directory is renamed out of `public/worlds` before it is removed. The
 * rename is atomic and instant, so the world leaves the app the moment it is
 * called; if removing the bytes then fails, what is left behind is a stray
 * folder under `.artifacts` rather than a half-deleted world the scanner still
 * tries to read.
 */
export async function DELETE(request: NextRequest) {
  if (!LOCAL_TOOLS_ENABLED || !isAppRequest(request)) {
    return new NextResponse('Not found', { status: 404 })
  }

  const slug = new URL(request.url).searchParams.get('slug')
  if (!slug) return NextResponse.json({ error: 'Missing slug.' }, { status: 400 })

  const worldDir = worldDirPath(slug)
  if (!worldDir) return NextResponse.json({ error: 'Unknown world.' }, { status: 404 })
  if (!fs.existsSync(worldDir) || !fs.statSync(worldDir).isDirectory()) {
    return NextResponse.json({ error: 'Unknown world.' }, { status: 404 })
  }

  const graveyard = path.join(process.cwd(), '.artifacts', 'deleted-worlds')
  const staged = path.join(graveyard, `${slug}-${Date.now()}`)

  try {
    fs.mkdirSync(graveyard, { recursive: true })
    fs.renameSync(worldDir, staged)
  } catch (error) {
    // A cross-device rename cannot happen while both live in the repository,
    // but a locked file can. Fall back to removing in place.
    console.warn(`[Worlds] Could not stage "${slug}" for deletion, removing in place:`, error)
    try {
      fs.rmSync(worldDir, { recursive: true, force: true })
      console.log(`[Worlds] Deleted "${slug}".`)
      return NextResponse.json({ slug })
    } catch (removeError) {
      console.error(`[Worlds] Could not delete "${slug}":`, removeError)
      return NextResponse.json({ error: 'Could not delete this world.' }, { status: 500 })
    }
  }

  try {
    fs.rmSync(staged, { recursive: true, force: true })
  } catch (error) {
    // The world is already gone as far as the app is concerned.
    console.warn(`[Worlds] "${slug}" was removed from the library but its files remain at ${staged}:`, error)
  }

  console.log(`[Worlds] Deleted "${slug}".`)
  return NextResponse.json({ slug })
}
