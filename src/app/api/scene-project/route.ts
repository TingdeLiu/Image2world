import { NextRequest, NextResponse } from 'next/server'
import fs from 'fs'
import path from 'path'
import {
  readSceneProject,
  sceneProjectPath,
  sanitizePlacementProject
} from '../../../utils/worldsScanner'

export const dynamic = 'force-dynamic'

export async function GET(request: NextRequest) {
  const { searchParams } = new URL(request.url)
  const slug = searchParams.get('slug')

  if (!slug) {
    return new NextResponse('Missing slug', { status: 400 })
  }

  const project = readSceneProject(slug)
  if (!project) {
    return new NextResponse('Not found', { status: 404 })
  }

  return NextResponse.json(project, {
    headers: {
      'Cache-Control': 'no-store, max-age=0, must-revalidate',
    },
  })
}

export async function POST(request: NextRequest) {
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
    fs.writeFileSync(filePath, `${JSON.stringify(project, null, 2)}\n`)

    return NextResponse.json(project)
  } catch (error) {
    console.error('Failed to save scene project:', error)
    return new NextResponse('Invalid JSON or write error', { status: 400 })
  }
}
