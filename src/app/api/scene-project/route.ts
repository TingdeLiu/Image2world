import { NextRequest, NextResponse } from 'next/server'
import fs from 'fs'
import path from 'path'
import {
  readSceneProjectResult,
  sceneProjectPath,
  sanitizePlacementProject
} from '../../../utils/worldsScanner'
import { guardRoute } from '../../../utils/routeGuard'

export const dynamic = 'force-dynamic'

export async function GET(request: NextRequest) {
  const { searchParams } = new URL(request.url)
  const slug = searchParams.get('slug')

  if (!slug) {
    return new NextResponse('Missing slug', { status: 400 })
  }

  const result = readSceneProjectResult(slug)
  // 404 means "no scene yet, go ahead and make one"; the editor acts on it.
  // A file that exists but will not parse has to say so instead, or that same
  // reply tells the editor to overwrite the user's arrangement with defaults.
  if (result.status === 'invalid') {
    return NextResponse.json({ error: `scene.json could not be read: ${result.detail}` }, { status: 422 })
  }
  if (result.status === 'absent') {
    return new NextResponse('Not found', { status: 404 })
  }
  const project = result.project

  return NextResponse.json(project, {
    headers: {
      'Cache-Control': 'no-store, max-age=0, must-revalidate',
    },
  })
}

export async function POST(request: NextRequest) {
  // This overwrites the user's saved scene. Another tab could otherwise post
  // an empty instance list here and wipe a room they spent an hour arranging.
  const { denied } = await guardRoute(request)
  if (denied) return denied

  const { searchParams } = new URL(request.url)
  const slug = searchParams.get('slug')

  if (!slug) {
    return new NextResponse('Missing slug', { status: 400 })
  }

  const filePath = sceneProjectPath(slug)
  if (!filePath) {
    return new NextResponse('Invalid slug', { status: 400 })
  }

  try {
    const body = await request.json()
    const project = sanitizePlacementProject(body)
    if (!project) {
      return new NextResponse('Invalid project', { status: 400 })
    }

    fs.mkdirSync(path.dirname(filePath), { recursive: true })

    // Keep whatever is there if it will not read. The editor no longer writes
    // over a damaged scene on its own, but this is the last point before the
    // bytes are gone, and a copy costs nothing next to an afternoon of work.
    if (readSceneProjectResult(slug).status === 'invalid') {
      const backup = filePath.replace(/\.json$/, `.broken-${Date.now()}.json`)
      try {
        fs.copyFileSync(filePath, backup)
        console.warn(`[Scene] "${slug}" had an unreadable scene.json; kept a copy at ${path.basename(backup)}`)
      } catch (error) {
        console.error(`[Scene] Could not back up the unreadable scene.json for "${slug}":`, error)
        return NextResponse.json(
          { error: 'This world has a scene.json that cannot be read, and it could not be backed up.' },
          { status: 409 },
        )
      }
    }

    fs.writeFileSync(filePath, `${JSON.stringify(project, null, 2)}\n`)

    return NextResponse.json(project)
  } catch (error) {
    console.error('Failed to save scene project:', error)
    return new NextResponse('Invalid JSON or write error', { status: 400 })
  }
}
