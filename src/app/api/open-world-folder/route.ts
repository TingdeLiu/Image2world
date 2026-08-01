import { NextRequest, NextResponse } from 'next/server'
import fs from 'fs'
import path from 'path'
import { openFolder } from '../../../utils/worldsScanner'

export const dynamic = 'force-dynamic'

const worldsDir = path.join(process.cwd(), 'public', 'worlds')
const repoRoot = process.cwd()

export async function GET(request: NextRequest) {
  const { searchParams } = new URL(request.url)
  const slug = searchParams.get('slug')
  const target = searchParams.get('target')
  const asset = searchParams.get('asset')

  if (target === 'root') {
    openFolder(repoRoot)
    return new NextResponse(null, { status: 204 })
  }

  if (!slug) {
    return new NextResponse('Missing slug', { status: 400 })
  }

  const worldDir = path.resolve(worldsDir, slug)
  const isInsideWorlds = worldDir !== worldsDir && worldDir.startsWith(`${worldsDir}${path.sep}`)
  if (!isInsideWorlds) {
    return new NextResponse('Not found', { status: 404 })
  }

  const folderPath = (() => {
    if (target === 'scene') return worldDir
    if (target === 'world-asset') return path.join(worldDir, 'output', 'world')
    if (target === 'object-asset') return asset ? path.join(worldDir, 'output', asset) : undefined
    return worldDir
  })()

  if (!folderPath) {
    return new NextResponse('Missing asset', { status: 400 })
  }

  const resolvedFolderPath = path.resolve(folderPath)
  const isInsideWorld = resolvedFolderPath === worldDir || resolvedFolderPath.startsWith(`${worldDir}${path.sep}`)
  if (!isInsideWorld) {
    return new NextResponse('Not found', { status: 404 })
  }

  if (!fs.existsSync(resolvedFolderPath) || !fs.statSync(resolvedFolderPath).isDirectory()) {
    return new NextResponse('Not found', { status: 404 })
  }

  openFolder(resolvedFolderPath)
  return new NextResponse(null, { status: 204 })
}
