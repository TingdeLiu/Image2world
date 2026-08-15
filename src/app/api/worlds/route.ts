import fs from 'fs'
import path from 'path'
import { NextRequest, NextResponse } from 'next/server'
import { readWorlds, worldDirPath } from '../../../utils/worldsScanner'
import { LOCAL_TOOLS_ENABLED } from '../../../utils/localTools'
import { isAppRequest } from '../../../utils/sameOrigin'
import { moveDirectory, slugify } from '../../../utils/worldPublish'

export const dynamic = 'force-dynamic'

/**
 * Slugs with a rename or delete in flight.
 *
 * Disabling the buttons stops the same tab double-firing; it does nothing about
 * a second tab, and both operations here take long enough to invite one. Two
 * concurrent renames would have their `cpSync` calls writing the same target
 * files, which on Windows ends as EBUSY or as a truncated .ply -- so the second
 * caller is turned away rather than allowed to interleave.
 */
const inFlight = new Set<string>()
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
 * Rename a world.
 *
 * The slug is the directory name, the URL, and what the sidebar shows, so this
 * moves the folder. It also lives in two files inside it -- `project.json`'s
 * `slug` and each world manifest's `world_id` -- which are updated after the
 * move: a folder that renamed but kept a stale id inside is cosmetic, while the
 * reverse would be a world the router cannot find.
 */
export async function PATCH(request: NextRequest) {
  if (!LOCAL_TOOLS_ENABLED || !isAppRequest(request)) {
    return new NextResponse('Not found', { status: 404 })
  }

  const slug = new URL(request.url).searchParams.get('slug')
  if (!slug) return NextResponse.json({ error: 'Missing slug.' }, { status: 400 })

  const worldDir = worldDirPath(slug)
  if (!worldDir || !fs.existsSync(worldDir)) {
    return NextResponse.json({ error: 'Unknown world.' }, { status: 404 })
  }

  let name: string
  try {
    name = String(((await request.json()) as { name?: unknown }).name ?? '').trim()
  } catch {
    return NextResponse.json({ error: 'Expected a JSON body with a name.' }, { status: 400 })
  }
  if (!name || name.length > 80) {
    return NextResponse.json({ error: 'A name has to be 1-80 characters.' }, { status: 400 })
  }

  const nextSlug = slugify(name)
  const nextDir = nextSlug && worldDirPath(nextSlug)
  if (!nextSlug || !nextDir) {
    return NextResponse.json(
      { error: 'That name has no letters or digits in it.' },
      { status: 400 },
    )
  }
  if (nextSlug === slug) return NextResponse.json({ slug })
  if (fs.existsSync(nextDir)) {
    return NextResponse.json({ error: `“${nextSlug}” already exists.` }, { status: 409 })
  }
  if (inFlight.has(slug) || inFlight.has(nextSlug)) {
    return NextResponse.json({ error: 'This world is already being changed.' }, { status: 409 })
  }

  inFlight.add(slug)
  inFlight.add(nextSlug)
  try {
    const how = await moveDirectory(worldDir, nextDir)
    if (how === 'copied') {
      console.log(`[Worlds] Renaming "${slug}" had to copy; the dev server holds public/ open.`)
    }
  } catch (error) {
    console.error(`[Worlds] Could not rename "${slug}":`, error)
    return NextResponse.json({ error: 'Could not rename this world.' }, { status: 500 })
  } finally {
    inFlight.delete(slug)
    inFlight.delete(nextSlug)
  }

  const patchJson = (file: string, apply: (record: Record<string, unknown>) => void) => {
    if (!fs.existsSync(file)) return
    try {
      const record = JSON.parse(fs.readFileSync(file, 'utf-8')) as Record<string, unknown>
      apply(record)
      fs.writeFileSync(file, `${JSON.stringify(record, null, 2)}\n`)
    } catch (error) {
      // The move already happened and is what matters; a stale id inside only
      // affects what is displayed, so say so rather than fail the request.
      console.warn(`[Worlds] Renamed to "${nextSlug}" but could not update ${path.basename(file)}:`, error)
    }
  }

  patchJson(path.join(nextDir, 'project.json'), (record) => {
    record.slug = nextSlug
    record.display_name = name
    record.updated_at = new Date().toISOString()
  })

  const manifestDir = path.join(nextDir, 'output', 'world')
  if (fs.existsSync(manifestDir)) {
    for (const file of fs.readdirSync(manifestDir)) {
      if (!/^\d+-world\.json$/.test(file)) continue
      patchJson(path.join(manifestDir, file), (record) => {
        record.world_id = nextSlug
      })
    }
  }

  console.log(`[Worlds] Renamed "${slug}" to "${nextSlug}".`)
  return NextResponse.json({ slug: nextSlug })
}

/**
 * Delete a world and everything under it.
 *
 * Gated the same way "open world folder" is, and for the same reason: this acts
 * on the disk of whichever machine runs the server. A same-origin check is not
 * enough on its own here -- a visitor to a deployed instance is same-origin, and
 * nothing about this is recoverable for them or for the operator.
 *
 * Where it can, the directory is renamed out of `public/worlds` first, so the
 * world leaves the app in one step and the slow part happens somewhere the
 * scanner does not look. Where it cannot -- which is every time under
 * `next dev`, see below -- it is removed in place.
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

  // Renaming out of `public/worlds` first would make the world vanish from the
  // app atomically, before the slow part. Under `next dev` that rename is not
  // available -- the watcher on `public/` holds the directory open -- so this
  // tries it and removes in place when it cannot, rather than paying to copy
  // hundreds of megabytes on the way to deleting them.
  if (inFlight.has(slug)) {
    return NextResponse.json({ error: 'This world is already being changed.' }, { status: 409 })
  }
  inFlight.add(slug)

  const staged = path.join(process.cwd(), '.artifacts', 'deleted-worlds', `${slug}-${Date.now()}`)
  let target = worldDir
  try {
    fs.mkdirSync(path.dirname(staged), { recursive: true })
    fs.renameSync(worldDir, staged)
    target = staged
  } catch {
    // Expected in development. The removal below is the part that matters.
  }

  try {
    fs.rmSync(target, { recursive: true, force: true })
  } catch (error) {
    console.error(`[Worlds] Could not delete "${slug}":`, error)
    return NextResponse.json({ error: 'Could not delete this world.' }, { status: 500 })
  } finally {
    inFlight.delete(slug)
  }

  console.log(`[Worlds] Deleted "${slug}".`)
  return NextResponse.json({ slug })
}
